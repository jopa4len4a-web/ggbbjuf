"""Очередь пополнений чеками."""
import asyncio
import logging

from . import money
from .cheques import parse_link
from .core import Err

log = logging.getLogger("deposits")
FAIL_LIMIT = 5


class Deposits:
    def __init__(self, core, account):
        self.core = core
        self.db = core.db
        self.account = account
        self.queue = asyncio.Queue()

    def get(self, deposit_id):
        return self.db.one("SELECT * FROM deposits WHERE id=?", deposit_id)

    def _update(self, deposit_id, **fields):
        fields["updated"] = self.core.now()
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.run(f"UPDATE deposits SET {cols} WHERE id=?", *fields.values(), deposit_id)

    def submit(self, user_id, text) -> int:
        user = self.core.user(user_id)
        if not user or user["blocked"]:
            raise Err("Операция недоступна")
        link = parse_link(text)
        if not link:
            raise Err("Это не похоже на чек CryptoBot или xRocket. Пришлите ссылку вида "
                      "t.me/send?start=CQ… или t.me/xrocket?start=t_…")
        provider, bot, code = link
        if not self.account.ready:
            raise Err("Приём чеков временно недоступен. Не отправляйте чек никому и попробуйте позже")
        now = self.core.now()
        failed = self.db.one(
            "SELECT COUNT(*) n FROM deposits WHERE user_id=? AND status='failed' AND updated>?",
            user_id, now - 600)["n"]
        waiting = self.db.one(
            "SELECT COUNT(*) n FROM deposits WHERE user_id=? AND status IN ('queued','processing')",
            user_id)["n"]
        if failed >= FAIL_LIMIT or waiting >= 3:
            raise Err("Слишком много попыток. Подождите несколько минут")
        old = self.db.one("SELECT * FROM deposits WHERE provider=? AND code=?", provider, code)
        if old and old["status"] != "failed":
            raise Err("Этот чек уже отправлен на проверку или зачислен")
        if old:
            self.db.run("UPDATE deposits SET user_id=?, status='queued', note=NULL, reply=NULL, "
                        "updated=? WHERE id=?", user_id, now, old["id"])
            deposit_id = old["id"]
        else:
            deposit_id = self.db.run(
                "INSERT INTO deposits(user_id,provider,code,status,note,created,updated) "
                "VALUES(?,?,?,'queued',NULL,?,?)", user_id, provider, f"{code}", now, now).lastrowid
        self.queue.put_nowait((deposit_id, bot))
        return deposit_id

    def recover(self):
        """После перезапуска: недоделанное уходит на ручную проверку, очередь восстанавливается."""
        self.db.run("UPDATE deposits SET status='review', note='Перезапуск во время активации' "
                    "WHERE status='processing'")
        for row in self.db.all("SELECT id, provider FROM deposits WHERE status='queued' ORDER BY id"):
            self.queue.put_nowait((row["id"], "send" if row["provider"] == "cryptobot" else "xrocket"))

    async def process(self, deposit_id, bot):
        deposit = self.get(deposit_id)
        if not deposit or deposit["status"] != "queued":
            return
        sent = []

        def mark_sent():
            sent.append(True)
            self._update(deposit_id, status="processing")

        try:
            result = await self.account.activate(bot, deposit["code"], mark_sent)
        except Exception as exc:
            log.exception("Ошибка активации #%s", deposit_id)
            if sent:
                self._update(deposit_id, status="review", note=f"Сбой при активации: {exc}"[:300])
            else:
                self._update(deposit_id, status="failed",
                             note="Не удалось проверить чек, отправьте его ещё раз позже")
            await self.core.notify("deposit_updated", deposit_id)
            return
        limit = int(self.core.settings()["auto_credit_max"])
        if result.status == "ok" and (result.usd <= 0 or result.usd > limit):
            result.status = "review"
            result.note = f"Сумма {money.fmt(result.usd)} $ выше лимита автозачисления"
        with self.db.tx():
            self._update(deposit_id, status={"ok": "credited"}.get(result.status, result.status),
                         usd=result.usd or None, asset=result.asset, asset_amount=result.amount,
                         note=result.note, reply=result.reply)
            if result.status == "ok":
                self.core._move(deposit["user_id"], result.usd, f"Чек #{deposit_id}")
        await self.core.notify("deposit_updated", deposit_id)

    async def worker(self):
        while True:
            deposit_id, bot = await self.queue.get()
            try:
                await self.process(deposit_id, bot)
            except Exception:
                log.exception("deposit worker")

    async def resolve(self, actor, deposit_id, action, usd_raw=None):
        """Ручное решение владельца по чеку на проверке."""
        self.core.require_owner(actor)
        deposit = self.get(deposit_id)
        if not deposit or deposit["status"] != "review":
            raise Err("Чек уже обработан")
        if action == "credit":
            try:
                usd = money.parse(usd_raw)
            except ValueError as exc:
                raise Err("Введите сумму в $") from exc
            if not 0 < usd <= 10_000_000:
                raise Err("Сумма должна быть от 0,01 до 100 000 $")
            with self.db.tx():
                self._update(deposit_id, status="credited", usd=usd, note="Зачислено вручную")
                self.core._move(deposit["user_id"], usd, f"Чек #{deposit_id} (вручную)", actor)
        elif action == "reject":
            self._update(deposit_id, status="failed", note="Отклонён после проверки")
        else:
            raise Err("Неизвестное действие")
        await self.core.notify("deposit_updated", deposit_id)

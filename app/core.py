"""Бизнес-логика обменника. Не знает ничего о Telegram: уведомления уходят в notifier."""
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import money
from .cheques import parse_link

log = logging.getLogger("core")

DEFAULTS = {
    "rate": "0",              # копеек за 1 $
    "currency": "₽",
    "min_usd": "1500",        # центы
    "max_usd": "500000",
    "accepting": "1",
    "hours_enabled": "0",
    "hours_from": "10:00",
    "hours_to": "22:00",
    "tz_offset": "3",
    "work_days": "1234567",
    "timer_min": "15",
    "auto_credit_max": "100000",  # чеки крупнее уходят на ручную проверку
}
MAX_OPEN_ORDERS = 3
PENDING = ("sent", "disputed")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{3,31}$")


class Err(Exception):
    """Ошибка, текст которой можно показать человеку."""


class Silent:
    def __getattr__(self, name):
        async def noop(*args, **kwargs):
            return None
        return noop


class Core:
    def __init__(self, db, config, notifier=None, clock=time.time):
        self.db = db
        self.config = config
        self.notifier = notifier or Silent()
        self.clock = clock
        with db.tx():
            for key, value in DEFAULTS.items():
                db.run("INSERT OR IGNORE INTO settings VALUES(?,?)", key, value)
            db.run("DELETE FROM admins WHERE is_owner=1 AND username<>?", config.owner_username)
            db.run("INSERT OR IGNORE INTO admins(username,is_owner,added) VALUES(?,1,?)",
                   config.owner_username, self.now())
            db.run("UPDATE admins SET is_owner=1 WHERE username=?", config.owner_username)
            if config.owner_id:
                db.run("UPDATE admins SET user_id=NULL WHERE user_id=? AND is_owner=0", config.owner_id)
                db.run("UPDATE admins SET user_id=? WHERE is_owner=1", config.owner_id)

    def now(self) -> int:
        return int(self.clock())

    async def notify(self, event, *args):
        try:
            await getattr(self.notifier, event)(*args)
        except Exception:
            log.exception("Не удалось отправить уведомление %s", event)

    # ---------- пользователи и роли ----------
    def touch_user(self, user_id: int, username: str | None, name: str):
        username = (username or "").lower() or None
        with self.db.tx():
            self.db.run(
                "INSERT INTO users(id,username,name,created) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name",
                user_id, username, name[:120], self.now())
            # Роль привязывается к числовому ID при первом входе: смена юзернейма её не передаёт.
            if username and not self.db.one("SELECT 1 FROM admins WHERE user_id=?", user_id):
                self.db.run("UPDATE admins SET user_id=? WHERE username=? AND user_id IS NULL",
                            user_id, username)
        return self.user(user_id)

    def user(self, user_id):
        return self.db.one("SELECT * FROM users WHERE id=?", user_id)

    def role(self, user_id) -> str | None:
        row = self.db.one("SELECT is_owner FROM admins WHERE user_id=?", user_id)
        if not row:
            return None
        return "owner" if row["is_owner"] else "admin"

    def require_staff(self, user_id):
        if not self.role(user_id):
            raise Err("Нужны права администратора")

    def require_owner(self, user_id):
        if self.role(user_id) != "owner":
            raise Err("Доступно только владельцу")

    def staff_ids(self):
        return [r["user_id"] for r in self.db.all("SELECT user_id FROM admins WHERE user_id IS NOT NULL")]

    def owner_ids(self):
        return [r["user_id"] for r in
                self.db.all("SELECT user_id FROM admins WHERE is_owner=1 AND user_id IS NOT NULL")]

    def admins(self):
        return self.db.all(
            "SELECT a.username, a.user_id, a.is_owner, u.name FROM admins a "
            "LEFT JOIN users u ON u.id=a.user_id ORDER BY a.is_owner DESC, a.added")

    @staticmethod
    def clean_username(raw) -> str:
        name = str(raw or "").strip()
        name = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)", "", name).lstrip("@").lower()
        if not USERNAME_RE.match(name):
            raise Err("Некорректный юзернейм")
        return name

    def add_admin(self, actor, username):
        self.require_owner(actor)
        name = self.clean_username(username)
        if self.db.one("SELECT 1 FROM admins WHERE username=?", name):
            raise Err("Этот администратор уже добавлен")
        self.db.run("INSERT INTO admins(username,added) VALUES(?,?)", name, self.now())

    async def remove_admin(self, actor, username):
        self.require_owner(actor)
        name = self.clean_username(username)
        row = self.db.one("SELECT * FROM admins WHERE username=?", name)
        if not row:
            raise Err("Администратор не найден")
        if row["is_owner"]:
            raise Err("Владельца удалить нельзя")
        released = []
        with self.db.tx():
            self.db.run("DELETE FROM admins WHERE username=?", name)
            if row["user_id"]:
                self.db.run("DELETE FROM states WHERE user_id=?", row["user_id"])
                for order in self.db.all(
                        "SELECT id FROM orders WHERE manager_id=? AND status='active'", row["user_id"]):
                    if self.db.one("SELECT 1 FROM payments WHERE order_id=? AND status<>'void'", order["id"]):
                        continue
                    self.db.run("UPDATE orders SET status='new', manager_id=NULL WHERE id=?", order["id"])
                    released.append(order["id"])
        for order_id in released:
            await self.notify("order_new", order_id)

    # ---------- настройки и часы работы ----------
    def settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.db.all("SELECT * FROM settings")}

    def work_state(self):
        """(можно ли создавать заявки, причина)."""
        s = self.settings()
        if int(s["rate"]) <= 0:
            return False, "Курс ещё не установлен"
        if s["accepting"] != "1":
            return False, "Приём заявок временно остановлен"
        if s["hours_enabled"] != "1":
            return True, ""
        tz = timezone(timedelta(hours=float(s["tz_offset"])))
        local = datetime.fromtimestamp(self.now(), tz)
        start, end, cur = s["hours_from"], s["hours_to"], local.strftime("%H:%M")
        day = local.isoweekday()
        if start == end:
            inside = True
        elif start < end:
            inside = start <= cur < end
        else:  # смена через полночь: утренняя часть относится к вчерашнему дню
            inside = cur >= start or cur < end
            if cur < end:
                day = 7 if day == 1 else day - 1
        if inside and str(day) in s["work_days"]:
            return True, ""
        return False, f"Мы работаем {start}–{end} (UTC{float(s['tz_offset']):+g})"

    def update_settings(self, actor, data: dict):
        self.require_staff(actor)
        if "auto_credit_max" in data and self.role(actor) != "owner":
            raise Err("Лимит автозачисления меняет только владелец")
        new = {}
        try:
            for key in ("rate", "min_usd", "max_usd", "auto_credit_max"):
                if key in data:
                    new[key] = money.parse(data[key])
            if "timer_min" in data:
                new["timer_min"] = int(data["timer_min"])
                if not 1 <= new["timer_min"] <= 180:
                    raise Err("Таймер: от 1 до 180 минут")
            if "tz_offset" in data:
                new["tz_offset"] = float(data["tz_offset"])
                if not -12 <= new["tz_offset"] <= 14:
                    raise Err("Часовой пояс: от -12 до +14")
        except (ValueError, TypeError) as exc:
            raise Err("Проверьте числа в настройках") from exc
        for key in ("hours_from", "hours_to"):
            if key in data:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(data[key])):
                    raise Err("Время в формате ЧЧ:ММ")
                new[key] = data[key]
        for key in ("accepting", "hours_enabled"):
            if key in data:
                new[key] = "1" if data[key] in (True, 1, "1") else "0"
        if "work_days" in data:
            new["work_days"] = "".join(sorted(set(re.findall(r"[1-7]", str(data["work_days"])))))
        if "currency" in data:
            new["currency"] = str(data["currency"]).strip()[:8] or "₽"
        merged = {**self.settings(), **{k: str(v) for k, v in new.items()}}
        low, high = int(merged["min_usd"]), int(merged["max_usd"])
        if low < 100:
            raise Err("Минимальный ордер не может быть меньше 1 $")
        if high < low:
            raise Err("Максимум меньше минимума")
        if "rate" in new:
            old = int(self.settings()["rate"])
            if new["rate"] <= 0 or new["rate"] > 10_000_000_00:
                raise Err("Курс должен быть больше нуля")
            # Защита от опечатки: резкое изменение курса нужно подтвердить.
            if old and not (old * 0.8 <= new["rate"] <= old * 1.2) and not data.get("confirm_rate"):
                raise Err("RATE_CONFIRM")
        with self.db.tx():
            for key, value in new.items():
                self.db.run("UPDATE settings SET value=? WHERE key=?", str(value), key)

    # ---------- баланс ----------
    def _move(self, user_id, delta, reason, actor=None):
        """Вызывать только внутри tx()."""
        cur = self.db.run("UPDATE users SET balance=balance+? WHERE id=? AND balance+?>=0",
                          delta, user_id, delta)
        if cur.rowcount != 1:
            raise Err("Недостаточно средств на балансе")
        self.db.run("INSERT INTO ledger(user_id,delta,reason,actor_id,created) VALUES(?,?,?,?,?)",
                    user_id, delta, reason, actor, self.now())

    async def adjust_balance(self, actor, who, delta_raw, reason):
        self.require_owner(actor)
        text = str(delta_raw).strip()
        sign = -1 if text.startswith("-") else 1
        try:
            delta = sign * money.parse(text.lstrip("+-"))
        except ValueError as exc:
            raise Err("Некорректная сумма") from exc
        if delta == 0 or abs(delta) > 10_000_000:
            raise Err("Сумма должна быть от 0,01 до 100 000 $")
        who = str(who).strip()
        if who.isdigit():
            target = self.user(int(who))
        else:
            rows = self.db.all("SELECT * FROM users WHERE username=?", self.clean_username(who))
            if len(rows) > 1:
                raise Err("Несколько пользователей с этим юзернеймом, укажите ID")
            target = rows[0] if rows else None
        if not target:
            raise Err("Пользователь не найден (он должен запустить бота)")
        reason = str(reason or "").strip()[:200]
        if not reason:
            raise Err("Укажите причину")
        with self.db.tx():
            self._move(target["id"], delta, "Корректировка: " + reason, actor)
        await self.notify("balance_changed", target["id"], delta, reason)

    # ---------- заявки ----------
    def order(self, order_id):
        return self.db.one("SELECT * FROM orders WHERE id=?", order_id)

    def payments(self, order_id):
        return self.db.all("SELECT * FROM payments WHERE order_id=? ORDER BY id", order_id)

    def payment(self, payment_id):
        return self.db.one("SELECT * FROM payments WHERE id=?", payment_id)

    def sums(self, order_id):
        row = self.db.one(
            "SELECT COALESCE(SUM(CASE WHEN status<>'void' THEN amount END),0) given, "
            "COALESCE(SUM(CASE WHEN status='received' THEN amount END),0) received "
            "FROM payments WHERE order_id=?", order_id)
        return row["given"], row["received"]

    async def create_order(self, user_id, usd_raw, phone, bank, fio, rate_seen, request_key):
        user = self.user(user_id)
        if not user or user["blocked"]:
            raise Err("Операция недоступна")
        key = str(request_key or "")[:64]
        if len(key) < 8:
            raise Err("Обновите страницу и повторите")
        same = self.db.one("SELECT id FROM orders WHERE user_id=? AND request_key=?", user_id, key)
        if same:
            return same["id"]
        ok, why = self.work_state()
        if not ok:
            raise Err(why)
        s = self.settings()
        try:
            usd = money.parse(usd_raw)
        except ValueError as exc:
            raise Err("Введите сумму в долларах, например 25 или 25,50") from exc
        if usd < int(s["min_usd"]):
            raise Err(f"Минимальный ордер {money.fmt(int(s['min_usd']))} $")
        if usd > int(s["max_usd"]):
            raise Err(f"Максимальный ордер {money.fmt(int(s['max_usd']))} $")
        rate = int(s["rate"])
        if str(rate_seen) != str(rate):
            raise Err("Курс изменился. Проверьте новую сумму и отправьте ещё раз")
        digits = re.sub(r"[\s()\-]", "", str(phone or ""))
        if not re.fullmatch(r"\+?\d{10,15}", digits):
            raise Err("Укажите номер телефона: 10–15 цифр")
        bank = " ".join(str(bank or "").split())
        if not 2 <= len(bank) <= 60:
            raise Err("Укажите банк")
        fio = " ".join(str(fio or "").split())
        if not 5 <= len(fio) <= 100 or len(fio.split()) < 2 or re.search(r"\d", fio):
            raise Err("Укажите ФИО полностью (минимум имя и фамилия)")
        count = self.db.one(
            "SELECT COUNT(*) n FROM orders WHERE user_id=? AND status IN ('new','active')", user_id)["n"]
        if count >= MAX_OPEN_ORDERS:
            raise Err(f"Не больше {MAX_OPEN_ORDERS} открытых заявок")
        now = self.now()
        with self.db.tx():
            cur = self.db.run(
                "INSERT INTO orders(user_id,usd,rate,total,currency,phone,bank,fio,status,request_key,"
                "created,updated) VALUES(?,?,?,?,?,?,?,?,'new',?,?,?)",
                user_id, usd, rate, money.convert(usd, rate), s["currency"], digits, bank, fio, key, now, now)
            order_id = cur.lastrowid
            self._move(user_id, -usd, f"Заявка #{order_id}")
        await self.notify("order_new", order_id)
        return order_id

    def _managed(self, actor, order_id, statuses=("active",)):
        self.require_staff(actor)
        order = self.order(order_id)
        if not order:
            raise Err("Заявка не найдена")
        if order["status"] not in statuses:
            raise Err("Заявка уже в другом статусе")
        if order["status"] == "active" and order["manager_id"] != actor and self.role(actor) != "owner":
            raise Err("Эту заявку ведёт другой менеджер")
        return order

    def _owned(self, user_id, order_id):
        order = self.order(order_id)
        if not order or order["user_id"] != user_id:
            raise Err("Заявка не найдена")
        return order

    def _set_order(self, order_id, **fields):
        fields["updated"] = self.now()
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.run(f"UPDATE orders SET {cols} WHERE id=?", *fields.values(), order_id)

    async def accept(self, actor, order_id):
        self.require_staff(actor)
        order = self.order(order_id)
        if not order:
            raise Err("Заявка не найдена")
        if order["user_id"] == actor and self.role(actor) != "owner":
            raise Err("Нельзя принять собственную заявку")
        cur = self.db.run(
            "UPDATE orders SET status='active', manager_id=?, updated=? WHERE id=? AND status='new'",
            actor, self.now(), order_id)
        if cur.rowcount != 1:
            raise Err("Заявку уже взяли или закрыли")
        await self.notify("order_accepted", order_id)

    async def release(self, actor, order_id):
        self._managed(actor, order_id)
        if self.sums(order_id)[0]:
            raise Err("По заявке уже есть выплаты, вернуть в очередь нельзя")
        with self.db.tx():
            self._set_order(order_id, status="new", manager_id=None)
        await self.notify("order_new", order_id)

    def check_amount(self, actor, order_id, amount_raw) -> int:
        """Защита от лишнего нуля: нельзя выдать больше остатка по заявке."""
        order = self._managed(actor, order_id)
        try:
            amount = money.parse(amount_raw)
        except ValueError as exc:
            raise Err("Ошибка: введите число, например 30000") from exc
        if amount <= 0:
            raise Err("Ошибка: сумма должна быть больше нуля")
        if any(p["status"] in PENDING for p in self.payments(order_id)):
            raise Err("Сначала дождитесь ответа по предыдущей выплате или отмените её")
        left = order["total"] - self.sums(order_id)[0]
        if amount > left:
            raise Err(f"Ошибка: слишком большая сумма. Остаток по заявке "
                      f"{money.fmt(left)} {order['currency']}, вы ввели {money.fmt(amount)}")
        return amount

    async def give(self, actor, order_id, amount_raw):
        amount = self.check_amount(actor, order_id, amount_raw)
        now = self.now()
        minutes = int(self.settings()["timer_min"])
        with self.db.tx():
            cur = self.db.run(
                "INSERT INTO payments(order_id,amount,status,deadline,created) VALUES(?,?,'sent',?,?)",
                order_id, amount, now + minutes * 60, now)
            self._set_order(order_id)
        await self.notify("payment_sent", cur.lastrowid)
        return cur.lastrowid

    async def void_payment(self, actor, payment_id):
        payment = self.payment(payment_id)
        if not payment:
            raise Err("Выплата не найдена")
        self._managed(actor, payment["order_id"])
        if payment["status"] not in PENDING:
            raise Err("Эту выплату уже нельзя отменить")
        if payment["status"] == "disputed" and self.role(actor) != "owner":
            raise Err("Спорную выплату может отменить только владелец")
        self.db.run("UPDATE payments SET status='void' WHERE id=?", payment_id)
        await self.notify("payment_void", payment_id)

    async def received(self, user_id, payment_id):
        payment = self.payment(payment_id)
        if not payment:
            raise Err("Выплата не найдена")
        order = self._owned(user_id, payment["order_id"])
        if order["status"] != "active" or payment["status"] not in PENDING:
            raise Err("Ответ по этой выплате уже не требуется")
        self.db.run("UPDATE payments SET status='received' WHERE id=?", payment_id)
        await self.notify("payment_received", payment_id)

    async def not_received(self, user_id, payment_id):
        payment = self.payment(payment_id)
        if not payment:
            raise Err("Выплата не найдена")
        order = self._owned(user_id, payment["order_id"])
        if order["status"] != "active" or payment["status"] not in PENDING:
            raise Err("Ответ по этой выплате уже не требуется")
        if payment["status"] == "disputed":
            return payment["video_code"]
        code = f"{secrets.randbelow(10000):04d}"
        self.db.run("UPDATE payments SET status='disputed', video_code=? WHERE id=?", code, payment_id)
        await self.notify("payment_disputed", payment_id)
        return code

    def awaiting_video(self, user_id):
        return self.db.one(
            "SELECT p.* FROM payments p JOIN orders o ON o.id=p.order_id WHERE o.user_id=? "
            "AND o.status='active' AND p.status='disputed' ORDER BY p.video_received, p.id DESC LIMIT 1",
            user_id)

    def mark_video(self, payment_id):
        self.db.run("UPDATE payments SET video_received=1 WHERE id=?", payment_id)

    async def complete(self, actor, order_id, force=False):
        order = self._managed(actor, order_id)
        if force:
            self.require_owner(actor)
        else:
            payments = [p for p in self.payments(order_id) if p["status"] != "void"]
            given = self.sums(order_id)[0]
            if given != order["total"]:
                raise Err(f"Выдано {money.fmt(given)} из {money.fmt(order['total'])} {order['currency']}")
            if any(p["status"] == "disputed" for p in payments):
                raise Err("Есть спор «Не пришло». Закрыть может клиент («Пришло») или владелец")
            if any(p["status"] == "sent" and p["deadline"] > self.now() for p in payments):
                raise Err("Таймер ещё идёт: дождитесь ответа клиента")
        with self.db.tx():
            self._set_order(order_id, status="done")
            self.db.run("DELETE FROM states WHERE order_id=?", order_id)
        await self.notify("order_done", order_id)

    async def cancel(self, actor, order_id, force=False):
        """Отмена менеджером с возвратом невыплаченной части на баланс."""
        order = self._managed(actor, order_id, ("new", "active"))
        if force:
            self.require_owner(actor)
        elif any(p["status"] in PENDING for p in self.payments(order_id)):
            raise Err("Есть неподтверждённая выплата. Отмените её или дождитесь ответа клиента")
        await self._close_cancel(order, actor)

    async def cancel_by_user(self, user_id, order_id):
        order = self._owned(user_id, order_id)
        if order["status"] != "new":
            raise Err("Заявку уже взяли в работу, напишите менеджеру")
        await self._close_cancel(order, user_id)

    async def _close_cancel(self, order, actor):
        received = self.sums(order["id"])[1]
        refund = order["usd"] * (order["total"] - received) // order["total"] if order["total"] else order["usd"]
        with self.db.tx():
            self._set_order(order["id"], status="cancelled", refunded=refund)
            if refund:
                self._move(order["user_id"], refund, f"Возврат по заявке #{order['id']}", actor)
            self.db.run("UPDATE payments SET status='void' WHERE order_id=? AND status IN ('sent','disputed')",
                        order["id"])
            self.db.run("DELETE FROM states WHERE order_id=?", order["id"])
        await self.notify("order_cancelled", order["id"], actor)

    async def expire_timers(self):
        rows = self.db.all(
            "SELECT p.id FROM payments p JOIN orders o ON o.id=p.order_id WHERE p.status='sent' "
            "AND o.status='active' AND p.expired_notified=0 AND p.deadline<=?", self.now())
        for row in rows:
            self.db.run("UPDATE payments SET expired_notified=1 WHERE id=?", row["id"])
            await self.notify("payment_expired", row["id"])

    # ---------- переписка ----------
    def chat_side(self, user_id, order_id):
        """Возвращает (заявка, пишет_ли_сотрудник) или Err."""
        order = self.order(order_id)
        if not order:
            raise Err("Заявка не найдена")
        if order["status"] not in ("new", "active"):
            raise Err("Заявка закрыта, переписка недоступна")
        if order["user_id"] == user_id and order["manager_id"] != user_id:
            return order, False
        self._managed(user_id, order_id, ("active",))
        return order, True

    async def add_message(self, sender, order_id, text, kind="text", source=None):
        order, staff = self.chat_side(sender, order_id)
        text = str(text or "").strip()[:2000]
        if kind == "text" and not text:
            raise Err("Пустое сообщение")
        if not staff and parse_link(text):
            raise Err("Не отправляйте чек в переписку: пополните им баланс (пришлите боту без режима ответа)")
        recent = self.db.one("SELECT COUNT(*) n FROM messages WHERE sender_id=? AND created>?",
                             sender, self.now() - 60)["n"]
        if recent >= 20:
            raise Err("Слишком часто, подождите минуту")
        cur = self.db.run(
            "INSERT INTO messages(order_id,sender_id,from_staff,kind,text,created) VALUES(?,?,?,?,?,?)",
            order_id, sender, int(staff), kind, text, self.now())
        await self.notify("chat_message", cur.lastrowid, source)

    def messages(self, order_id):
        return self.db.all("SELECT * FROM messages WHERE order_id=? ORDER BY id", order_id)

    # ---------- выдача данных ----------
    def order_view(self, viewer, order_id):
        order = self.order(order_id)
        if not order:
            raise Err("Заявка не найдена")
        role = self.role(viewer)
        mine = order["user_id"] == viewer
        manages = bool(role) and (role == "owner" or order["manager_id"] in (None, viewer))
        if not mine and not manages:
            raise Err("Заявка не найдена")
        client = self.user(order["user_id"])
        manager = self.user(order["manager_id"]) if order["manager_id"] else None
        given, received = self.sums(order_id)
        payments = self.payments(order_id)
        if not manages:
            payments = [p for p in payments if p["status"] != "void"]
        view = {k: v for k, v in order.items() if k != "request_key"}
        view.update(
            given=given, received=received, payments=payments, messages=self.messages(order_id),
            mine=mine, manages=manages,
            client={"id": client["id"], "username": client["username"], "name": client["name"]},
            manager=({"username": manager["username"], "name": manager["name"]}
                     if manager and manages else None))
        return view

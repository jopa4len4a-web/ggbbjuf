"""Диалоги бота и уведомления."""
import asyncio
import html
import json
import logging

from . import emoji, money
from .cheques import parse_link
from .core import Err
from .telegram import TelegramError

log = logging.getLogger("bot")
STATE_TTL = 15 * 60
VIDEO_KINDS = ("video", "video_note")
MEDIA = {"photo": "фото", "video": "видео", "video_note": "видеосообщение", "voice": "голосовое",
         "document": "файл", "audio": "аудио"}
# Сообщение с этой кнопкой удаляется после успешного нажатия.
ONE_SHOT = {"acc", "rej", "rejok", "give", "done", "doneok", "void", "got", "nogot",
            "lost", "came", "lostok", "cameok", "ok", "no"}
SENDERS = {"photo": "sendPhoto", "video": "sendVideo", "video_note": "sendVideoNote", "voice": "sendVoice",
           "document": "sendDocument", "audio": "sendAudio"}
DEPOSIT_STATUS = {"queued": "в очереди", "processing": "проверяется", "credited": "зачислен",
                  "failed": "не принят", "review": "на ручной проверке"}


def esc(value):
    return html.escape(str(value if value is not None else ""))


def button(text, data=None, style=None, url=None, web_app=None):
    item = {"text": text}
    if data:
        item["callback_data"] = data
    if url:
        item["url"] = url
    if web_app:
        item["web_app"] = {"url": web_app}
    if style:
        item["style"] = style  # success — зелёная, danger — красная, primary — синяя
    return item


def keyboard(*rows):
    return {"inline_keyboard": [list(r) for r in rows if r]}


class Bot:
    def __init__(self, api, core, deposits):
        self.api = api
        self.core = core
        self.db = core.db
        self.deposits = deposits

    # ---------- отправка ----------
    async def send(self, chat_id, text, markup=None, order_id=None, tag="note"):
        try:
            message = await self.post(chat_id, text, markup)
        except TelegramError as exc:
            log.warning("sendMessage %s: %s", chat_id, exc)
            return None
        if order_id and message:
            self.track(chat_id, message["message_id"], order_id, tag)
        return message

    # ---------- чистота чата ----------
    def track(self, chat_id, message_id, order_id, tag):
        """Запоминает сообщение сделки, чтобы потом удалить его."""
        self.db.run("INSERT OR REPLACE INTO reply_map VALUES(?,?,?)", chat_id, message_id, order_id)
        self.db.run("INSERT OR REPLACE INTO deal_msgs VALUES(?,?,?,?)", chat_id, message_id, order_id, tag)

    async def delete(self, chat_id, message_ids):
        for start in range(0, len(message_ids), 100):
            try:
                await self.api.call("deleteMessages", chat_id=chat_id,
                                    message_ids=sorted(message_ids[start:start + 100]))
            except TelegramError as exc:  # старше 48 часов Telegram удалить не даёт
                log.info("deleteMessages %s: %s", chat_id, exc)
        for message_id in message_ids:
            self.db.run("DELETE FROM deal_msgs WHERE chat_id=? AND message_id=?", chat_id, message_id)

    async def purge(self, order_id, chat_id=None, tag=None):
        sql, args = "SELECT chat_id, message_id FROM deal_msgs WHERE order_id=?", [order_id]
        if chat_id is not None:
            sql, args = sql + " AND chat_id=?", args + [chat_id]
        if tag is not None:
            sql, args = sql + " AND tag=?", args + [tag]
        chats = {}
        for row in self.db.all(sql, *args):
            chats.setdefault(row["chat_id"], []).append(row["message_id"])
        for chat, ids in chats.items():
            await self.delete(chat, ids)

    async def post(self, chat_id, text, markup=None):
        """sendMessage с премиум эмодзи; если Telegram их не принял — обычными."""
        style = emoji.Decorator(self.core.settings()["emoji_map"])
        variants = [(style.text(text), style.markup(markup))]
        if variants[0] != (text, markup):
            variants.append((text, markup))
        for number, (body, buttons) in enumerate(variants, 1):
            try:
                return await self.api.call("sendMessage", chat_id=chat_id, text=body, parse_mode="HTML",
                                           reply_markup=buttons, link_preview_options={"is_disabled": True})
            except TelegramError as exc:
                if exc.code != 400 or number == len(variants):
                    raise

    async def load_emoji(self, actor, link):
        self.core.require_owner(actor)
        if not str(link or "").strip():
            self.core.set_setting("emoji_pack", "")
            self.core.set_setting("emoji_map", "{}")
            return 0
        name = emoji.pack_name(link)
        try:
            pack = await self.api.call("getStickerSet", name=name)
        except TelegramError:
            raise Err("Набор не найден или Telegram не отвечает") from None
        if pack.get("sticker_type") != "custom_emoji":
            raise Err("Это набор стикеров. Нужен набор эмодзи (t.me/addemoji/…)")
        mapping = emoji.build_map(pack.get("stickers", []))
        self.core.set_setting("emoji_pack", name)
        self.core.set_setting("emoji_map", json.dumps(mapping, ensure_ascii=False))
        return len(mapping)

    # ---------- рассылка ----------
    async def broadcast_step(self) -> bool:
        """Отправляет одно сообщение рассылки. False, если отправлять нечего."""
        job = self.db.one("SELECT * FROM broadcasts WHERE status='running' ORDER BY id LIMIT 1")
        if not job:
            return False
        user = self.db.one("SELECT id FROM users WHERE id>? AND id<=? AND blocked=0 ORDER BY id LIMIT 1",
                           job["last_user"], job["max_user"])
        if not user:
            self.db.run("UPDATE broadcasts SET status='done' WHERE id=?", job["id"])
            done = self.db.one("SELECT * FROM broadcasts WHERE id=?", job["id"])
            await self.send(job["actor_id"], f"📣 <b>Рассылка завершена</b>\n\n✅ Доставлено: {done['sent']}\n"
                                             f"🚫 Не доставлено: {done['failed']}")
            return True
        # Позиция сохраняется до отправки: после перезапуска никто не получит сообщение дважды.
        self.db.run("UPDATE broadcasts SET last_user=? WHERE id=?", user["id"], job["id"])
        markup = keyboard([self.app_button("🚀 Открыть TwaragaSwap")]) if job["button"] else None
        try:
            if job["message_id"]:
                await self.api.call("copyMessage", chat_id=user["id"], from_chat_id=job["from_chat"],
                                    message_id=job["message_id"], reply_markup=markup)
            else:
                await self.post(user["id"], esc(job["text"]), markup)
            column = "sent"
        except TelegramError as exc:
            log.info("broadcast %s: %s", user["id"], exc)
            column = "failed"
        self.db.run(f"UPDATE broadcasts SET {column}={column}+1 WHERE id=?", job["id"])
        return True

    async def broadcaster(self):
        while True:
            try:
                busy = await self.broadcast_step()
            except Exception:
                log.exception("broadcast")
                busy = False
            await asyncio.sleep(0.05 if busy else 2)  # не больше ~20 сообщений в секунду

    def app_button(self, text="Открыть TwaragaSwap", order_id=None):
        url = self.core.config.url + "/" + (f"?order={order_id}" if order_id else "")
        return button(text, web_app=url, style="primary")

    def staff_for(self, order):
        return [order["manager_id"]] if order["manager_id"] else self.core.staff_ids()

    # ---------- карточки ----------
    def order_card(self, order):
        client = self.core.user(order["user_id"])
        given, received = self.core.sums(order["id"])
        who = ("@" + client["username"]) if client["username"] else client["name"]
        status = {"new": "🆕 Новая", "active": "⏳ В работе", "done": "✅ Завершена",
                  "cancelled": "✖️ Отменена"}[order["status"]]
        cur = esc(order["currency"])
        lines = [
            f"<b>Заявка #{order['id']}</b>  ·  {status}",
            "",
            f"💵 <b>{money.fmt(order['usd'])} $</b>  →  <b>{money.fmt(order['total'])} {cur}</b>",
            f"<i>курс {money.fmt(order['rate'])}</i>",
            "",
            "<blockquote>"
            f"📱 <code>{esc(order['phone'])}</code>\n"
            f"🏦 {esc(order['bank'])}\n"
            f"👤 <code>{esc(order['fio'])}</code></blockquote>",
            f"Клиент: <a href=\"tg://user?id={client['id']}\">{esc(who)}</a> · <code>{client['id']}</code>",
        ]
        if order["manager_id"]:
            manager = self.core.user(order["manager_id"])
            lines.append("Менеджер: " + esc("@" + manager["username"] if manager and manager["username"]
                                            else order["manager_id"]))
        if given:
            lines.append(f"\n{self.progress(received, order['total'])}\n"
                         f"Выдано {money.fmt(given)} · подтверждено {money.fmt(received)} {cur}")
        if order["status"] in ("new", "active"):
            lines.append(self.left_line(order))
        if order["refunded"]:
            lines.append(f"↩️ Возвращено на баланс: <b>{money.fmt(order['refunded'])} $</b>")
        return "\n".join(lines)

    def left_line(self, order):
        return f"🧮 Остаток по заявке: <b>{money.fmt(self.core.left(order))} {esc(order['currency'])}</b>"

    @staticmethod
    def progress(part, total):
        filled = min(10, part * 10 // total) if total else 0
        return "🟩" * filled + "⬜" * (10 - filled)

    def staff_keyboard(self, order):
        oid = order["id"]
        if order["status"] == "new":
            return keyboard(
                [button("✅ Принять", f"acc:{oid}", "success"), button("✖️ Отклонить", f"rej:{oid}", "danger")],
                [self.app_button("📲 Открыть в панели", oid)])
        if order["status"] != "active":
            return keyboard()
        rows = []
        for p in self.core.payments(oid):
            amount = money.fmt(p["amount"])
            if p["status"] == "disputed":
                rows.append([button(f"❌ Деньги не пришли · {amount}", f"lost:{p['id']}", "danger")])
                rows.append([button(f"✅ Деньги пришли · {amount}", f"came:{p['id']}", "success")])
            elif p["status"] == "sent":
                rows.append([button(f"↩️ Отменить выплату {amount}", f"void:{p['id']}")])
        if self.core.left(order) > 0:
            rows.append([button("💸 Дать сумму", f"give:{oid}", "primary")])
        rows.append([button("🏁 Завершить", f"done:{oid}", "success"),
                     button("💬 Написать", f"chat:{oid}")])
        rows.append([{"text": "📋 Телефон", "copy_text": {"text": order["phone"]}},
                     {"text": "📋 ФИО", "copy_text": {"text": order["fio"][:256]}}])
        rows.append([self.app_button("📲 Открыть в панели", oid)])
        return keyboard(*rows)

    async def staff_card(self, chat_id, order, headline=""):
        """В чате сотрудника всегда одна актуальная карточка сделки."""
        await self.purge(order["id"], chat_id, "card")
        text = (headline + "\n\n" if headline else "") + self.order_card(order)
        await self.send(chat_id, text, self.staff_keyboard(order), order["id"], "card")

    # ---------- уведомления (вызываются из Core) ----------
    async def order_new(self, order_id):
        order = self.core.order(order_id)
        if order["created"] == order["updated"]:  # не повторяем при возврате в очередь
            await self.send(order["user_id"],
                            f"Заявка <b>#{order_id}</b> создана: {money.fmt(order['usd'])} $ → "
                            f"<b>{money.fmt(order['total'])} {esc(order['currency'])}</b>.\nЖдём менеджера.",
                            keyboard([self.app_button("Открыть заявку", order_id)]), order_id, "info")
        staff = self.core.staff_ids()
        if not staff:
            log.warning("Нет администраторов: пусть @%s нажмёт /start", self.core.config.owner_username)
        for chat_id in staff:
            await self.staff_card(chat_id, order, "🔔 Новая заявка")

    async def order_accepted(self, order_id):
        order = self.core.order(order_id)
        await self.purge(order_id, tag="card")  # у остальных админов уведомление исчезает
        await self.purge(order_id, order["user_id"], "info")
        await self.staff_card(order["manager_id"], order, "Вы приняли заявку")
        await self.send(order["user_id"], f"✅ Заявка <b>#{order_id}</b> принята. Менеджер готовит выплату.\n"
                                          f"{self.left_line(order)}",
                        keyboard([button("💬 Написать менеджеру", f"chat:{order_id}")],
                                 [self.app_button("Открыть заявку", order_id)]), order_id, "info")

    def payment_keyboard(self, payment):
        return keyboard([button("✅ Пришло", f"got:{payment['id']}", "success"),
                         button("❌ Не пришло", f"nogot:{payment['id']}", "danger")],
                        [button("💬 Написать менеджеру", f"chat:{payment['order_id']}")])

    async def _payment(self, payment_id):
        """Выплата и заявка; прежнее сообщение клиенту по этой выплате удаляется."""
        payment = self.core.payment(payment_id)
        order = self.core.order(payment["order_id"])
        await self.purge(order["id"], order["user_id"], f"pay{payment_id}")
        amount = f"{money.fmt(payment['amount'])} {esc(order['currency'])}"
        return payment, order, amount

    async def payment_sent(self, payment_id):
        payment, order, amount = await self._payment(payment_id)
        minutes = max(1, round((payment["deadline"] - payment["created"]) / 60))
        await self.send(order["user_id"],
                        f"💸 По заявке <b>#{order['id']}</b> отправлено <b>{amount}</b>\n"
                        f"{esc(order['bank'])}, <code>{esc(order['phone'])}</code>\n"
                        f"{self.left_line(order)}\n\n"
                        f"⏱ Проверьте поступление. «Не пришло» станет доступно через <b>{minutes} мин</b>.\n"
                        "Нажимайте «Пришло» только когда деньги действительно на счёте.",
                        self.payment_keyboard(payment), order["id"], f"pay{payment_id}")
        await self.staff_card(order["manager_id"], order, f"Выплата {amount} отмечена, таймер {minutes} мин")

    async def payment_received(self, payment_id):
        payment, order, amount = await self._payment(payment_id)
        await self.send(order["user_id"], f"✅ Получение {amount} по заявке #{order['id']} подтверждено.\n"
                                          f"{self.left_line(order)}", order_id=order["id"], tag="info")
        await self.staff_card(order["manager_id"], order, f"🟢 Клиент подтвердил: <b>пришло {amount}</b>")

    async def payment_disputed(self, payment_id):
        payment, order, amount = await self._payment(payment_id)
        await self.send(order["user_id"], self.video_text(payment["video_code"]),
                        keyboard([button("✅ Всё-таки пришло", f"got:{payment_id}", "success")],
                                 [button("💬 Написать менеджеру", f"chat:{order['id']}")]),
                        order["id"], f"pay{payment_id}")
        await self.staff_card(order["manager_id"], order,
                              f"🔴 Клиент: <b>НЕ пришло {amount}</b>\n"
                              f"Код для видео: <code>{payment['video_code']}</code>")

    async def dispute_resolved(self, payment_id, actor):
        payment, order, amount = await self._payment(payment_id)
        verdict = "деньги пришли" if payment["status"] == "received" else "деньги не пришли"
        await self.send(order["user_id"], f"⚖️ Спор по выплате {amount} (заявка #{order['id']}) решён: "
                                          f"<b>{verdict}</b>.\n{self.left_line(order)}",
                        keyboard([button("💬 Написать менеджеру", f"chat:{order['id']}")]),
                        order["id"], "info")
        await self.staff_card(order["manager_id"], order, f"⚖️ Спор по {amount} решён: {verdict}")
        if self.core.role(actor) != "owner":  # владелец видит все решения по спорам
            user = self.core.user(actor)
            who = "@" + user["username"] if user["username"] else str(actor)
            for chat_id in self.core.owner_ids():
                await self.send(chat_id, f"⚖️ {esc(who)} решил спор по заявке #{order['id']}, {amount}: "
                                         f"<b>{verdict}</b>",
                                keyboard([self.app_button("📂 Открыть сделку", order["id"])]))

    @staticmethod
    def video_text(code):
        return (f"Ваш код проверки: <code>{code}</code>\n\n"
                "Запишите <b>видео экрана</b> одним дублем и пришлите его сюда:\n"
                f"1. Покажите код <code>{code}</code>.\n"
                "2. Откройте многозадачность (список запущенных приложений) и из неё перейдите в банк.\n"
                "3. Покажите свой номер телефона и ФИО в профиле банка.\n"
                "4. Откройте платежи и историю операций.\n"
                "5. Несколько раз обновите историю (потяните вниз).\n\n"
                "⚠️ Не показывайте пароли, коды из СМС, полный номер карты и CVV.")

    async def payment_expired(self, payment_id):
        payment, order, amount = await self._payment(payment_id)
        grace = esc(self.core.settings()["grace_min"])
        await self.send(order["user_id"],
                        f"⏱ Время по заявке #{order['id']} вышло. Выплата <b>{amount}</b> пришла?\n"
                        f"Ответьте в течение <b>{grace} мин</b>, иначе выплата может быть засчитана.",
                        self.payment_keyboard(payment), order["id"], f"pay{payment_id}")
        await self.staff_card(order["manager_id"], order, f"⏱ Таймер по {amount} истёк, клиент не ответил")

    async def payment_void(self, payment_id):
        payment, order, amount = await self._payment(payment_id)
        await self.send(order["user_id"], f"Выплата {amount} по заявке #{order['id']} отменена менеджером.\n"
                                          f"{self.left_line(order)}", order_id=order["id"], tag="info")
        await self.staff_card(order["manager_id"], order, "Выплата отменена")

    async def _final(self, order, text, extra=()):
        """Конец сделки: переписка в боте очищается, остаётся итог с кнопкой истории."""
        await self.purge(order["id"])
        markup = keyboard([self.app_button("📂 История сделки", order["id"])])
        await self.send(order["user_id"], text, markup)
        for chat_id in {order["manager_id"], *extra} - {None, order["user_id"]}:
            await self.send(chat_id, text, markup)

    async def order_done(self, order_id):
        order = self.core.order(order_id)
        cur = esc(order["currency"])
        paid = self.core.sums(order_id)[0]
        lines = [f"🏁 <b>Заявка #{order_id} завершена</b>", "",
                 f"Выплачено: <b>{money.fmt(paid)} {cur}</b> из {money.fmt(order['total'])} {cur}"]
        if order["refunded"]:
            lines.append(f"↩️ Остаток возвращён на баланс: <b>+{money.fmt(order['refunded'])} $</b>")
        await self._final(order, "\n".join(lines))

    async def order_cancelled(self, order_id, actor):
        order = self.core.order(order_id)
        await self._final(order, f"✖️ <b>Заявка #{order_id} отменена</b>\n\nНа баланс возвращено "
                                 f"<b>{money.fmt(order['refunded'])} $</b>.", (actor,))

    async def balance_changed(self, user_id, delta, reason):
        sign = "+" if delta > 0 else ""
        await self.send(user_id, f"Баланс изменён: <b>{sign}{money.fmt(delta)} $</b>\n{esc(reason)}")

    async def deposit_updated(self, deposit_id):
        dep = self.deposits.get(deposit_id)
        if dep["status"] == "credited":
            balance = self.core.user(dep["user_id"])["balance"]
            await self.send(dep["user_id"],
                            f"✅ Чек зачислен: <b>+{money.fmt(dep['usd'])} $</b>\n"
                            f"Баланс: <b>{money.fmt(balance)} $</b>", keyboard([self.app_button("Обменять")]))
        elif dep["status"] == "failed":
            await self.send(dep["user_id"], f"❌ Чек не принят: {esc(dep['note'])}")
        elif dep["status"] == "review":
            await self.send(dep["user_id"], f"🕓 Чек #{deposit_id} отправлен на ручную проверку.")
            for chat_id in self.core.owner_ids():
                await self.send(chat_id,
                                f"⚠️ Чек #{deposit_id} требует проверки\nКлиент ID <code>{dep['user_id']}</code>\n"
                                f"{esc(dep['note'])}\nОтвет бота: <code>{esc(dep['reply'] or '—')}</code>",
                                keyboard([self.app_button("Открыть панель")]))

    async def chat_message(self, message_id, source=None):
        msg = self.db.one("SELECT * FROM messages WHERE id=?", message_id)
        order = self.core.order(msg["order_id"])
        oid = order["id"]
        if msg["from_staff"]:
            targets, title = [order["user_id"]], f"💬 Менеджер · заявка #{oid}"
        else:
            targets, title = self.staff_for(order), f"💬 Клиент · заявка #{oid}"
        reply = keyboard([button("↩️ Ответить", f"chat:{oid}", "primary")])
        for chat_id in targets:
            if source:  # медиа пересылаем копией
                await self.send(chat_id, title, order_id=oid, tag="chat")
                try:
                    copied = await self.api.call("copyMessage", chat_id=chat_id, from_chat_id=source[0],
                                                 message_id=source[1], reply_markup=reply)
                    self.track(chat_id, copied["message_id"], oid, "chat")
                except TelegramError as exc:
                    log.warning("copyMessage: %s", exc)
            else:
                await self.send(chat_id, f"<b>{title}</b>\n{esc(msg['text'])}", reply, oid, "chat")

    async def send_files(self, chat_id, order_id):
        """Повторно присылает вложения из истории сделки."""
        self.core.order_view(chat_id, order_id)  # проверка доступа
        rows = self.db.all("SELECT kind, file_id, text, from_staff FROM messages WHERE order_id=? "
                           "AND file_id IS NOT NULL ORDER BY id", order_id)
        if not rows:
            raise Err("В этой сделке нет вложений")
        active = self.core.order(order_id)["status"] in ("new", "active")
        for row in rows[-20:]:
            kind = row["kind"] if row["kind"] in SENDERS else "document"
            try:
                sent = await self.api.call(SENDERS[kind], chat_id=chat_id, **{kind: row["file_id"]})
            except TelegramError as exc:
                log.warning("send file: %s", exc)
                continue
            if active:
                self.track(chat_id, sent["message_id"], order_id, "chat")

    # ---------- состояния ввода ----------
    def set_state(self, user_id, kind, order_id, amount=None, nonce=None):
        self.db.run("INSERT OR REPLACE INTO states VALUES(?,?,?,?,?,?)",
                    user_id, kind, order_id, amount, nonce, self.core.now() + STATE_TTL)

    def get_state(self, user_id):
        return self.db.one("SELECT * FROM states WHERE user_id=? AND expires>?", user_id, self.core.now())

    def clear_state(self, user_id):
        self.db.run("DELETE FROM states WHERE user_id=?", user_id)

    async def drop_input(self, uid):
        """Выход из режима ввода: убирает подсказки и возвращает карточку сотруднику."""
        state = self.get_state(uid)
        if not state or not state["order_id"]:
            return
        await self.purge(state["order_id"], uid, "prompt")
        await self.restore_card(uid, state["order_id"])

    async def restore_card(self, uid, order_id):
        order = self.core.order(order_id)
        if not order or not self.core.role(uid):
            return
        if (order["status"] == "new" or
                (order["status"] == "active" and uid in (order["manager_id"], *self.core.owner_ids()))):
            if order["user_id"] != uid or order["manager_id"] == uid:
                await self.staff_card(uid, order)

    # ---------- входящие ----------
    async def handle(self, update):
        if "callback_query" in update:
            await self.on_callback(update["callback_query"])
        elif "message" in update:
            await self.on_message(update["message"])

    def _touch(self, sender):
        name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or "—"
        self.core.touch_user(sender["id"], sender.get("username"), name)

    async def on_message(self, message):
        if message.get("chat", {}).get("type") != "private" or "from" not in message:
            return
        uid = message["from"]["id"]
        is_new = not self.core.user(uid)
        self._touch(message["from"])
        text = (message.get("text") or "").strip()
        try:
            if text in ("/cancel",) or text.startswith("/start"):
                await self.drop_input(uid)
            if text.startswith("/start"):
                self.clear_state(uid)
                payload = text.partition(" ")[2]
                if is_new and payload.startswith("ref_") and payload[4:].isdecimal() and len(payload) < 24:
                    self.core.bind_referral(uid, int(payload[4:]))
                await self.welcome(uid)
            elif text == "/cancel":
                self.clear_state(uid)
                await self.send(uid, "Ввод отменён.")
            elif text == "/balance":
                await self.show_balance(uid)
            elif text == "/ref":
                await self.show_referral(uid)
            elif text == "/broadcast":
                self.core.require_owner(uid)
                self.set_state(uid, "bcast", 0)
                await self.send(uid, "📣 Пришлите сообщение для рассылки: текст, фото или видео.\n/cancel — отмена")
            else:
                await self.free_input(uid, message, text)
        except Err as exc:
            await self.send(uid, esc(exc))

    def menu(self):
        row = [button("💰 Баланс", "bal")]
        if self.core.referral_enabled():
            row.append(button("🎁 Бонусы", "ref"))
        return keyboard([self.app_button("🚀 Открыть TwaragaSwap")], row,
                        [button("📖 Как это работает", "help")])

    async def welcome(self, uid):
        s = self.core.settings()
        ok, why = self.core.work_state()
        rate = f"1 $ = {money.fmt(int(s['rate']))} {esc(s['currency'])}" if int(s["rate"]) else "курс не задан"
        text = ("💎 <b>TwaragaSwap</b>\n"
                "<i>Чеки CryptoBot и xRocket → перевод по номеру телефона</i>\n\n"
                "<blockquote>"
                f"💱 Курс: <b>{rate}</b>\n"
                f"📉 Минимум: <b>{money.fmt(int(s['min_usd']))} $</b>\n"
                f"💰 Баланс: <b>{money.fmt(self.core.user(uid)['balance'])} $</b></blockquote>\n"
                f"{'🟢 Принимаем заявки' if ok else '🔴 ' + esc(why)}\n\n"
                "🧾 Пришлите ссылку на чек прямо сюда 👇")
        if self.core.role(uid):
            text += "\n\n👑 Вы администратор: панель находится в приложении."
        await self.send(uid, text, self.menu())

    async def show_balance(self, uid):
        await self.send(uid, f"💰 <b>Ваш баланс</b>\n\n💵 <b>{money.fmt(self.core.user(uid)['balance'])} $</b>",
                        keyboard([self.app_button("💱 Обменять")]))

    async def show_help(self, uid):
        s = self.core.settings()
        await self.send(uid, "📖 <b>Как это работает</b>\n\n"
                             "1️⃣ Создайте чек в USDT в @send или @xrocket без пароля\n"
                             "2️⃣ Пришлите ссылку сюда — деньги придут на баланс\n"
                             "3️⃣ В приложении укажите сумму, телефон, банк и ФИО\n"
                             "4️⃣ Менеджер отправит перевод\n"
                             f"5️⃣ Проверьте счёт за {esc(s['timer_min'])} мин и нажмите «Пришло» или «Не пришло»",
                        keyboard([self.app_button("🚀 Открыть TwaragaSwap")]))

    async def show_referral(self, uid):
        info = self.core.referral_info(uid)
        if not info:
            raise Err("Раздел сейчас отключён")
        rows = []
        if info["link"]:
            rows.append([{"text": "📋 Скопировать ссылку", "copy_text": {"text": info["link"]}}])
        await self.send(uid, "🎁 <b>Реферальная программа</b>\n\n"
                             f"Приглашайте друзей и получайте <b>{money.fmt(info['percent'])} %</b> "
                             "с каждого их обмена на баланс.\n\n"
                             f"<blockquote>👥 Приглашено: <b>{info['invited']}</b>\n"
                             f"💸 Заработано: <b>{money.fmt(info['earned'])} $</b></blockquote>\n"
                             f"🔗 <code>{esc(info['link'] or 'ссылка скоро появится')}</code>",
                        keyboard(*rows) if rows else None)

    async def referral_bonus(self, order_id):
        row = self.db.one("SELECT * FROM ref_earnings WHERE order_id=?", order_id)
        await self.send(row["referrer_id"],
                        f"🎁 <b>Реферальный бонус +{money.fmt(row['amount'])} $</b>\nЗачислен на баланс.")

    async def free_input(self, uid, message, text):
        kind = next((k for k in MEDIA if k in message), "text" if text else None)
        if not kind:
            return
        source = (uid, message["message_id"]) if kind != "text" else None
        body = text or message.get("caption") or f"[{MEDIA.get(kind, 'вложение')}]"

        link_text = " ".join([text, message.get("caption") or ""] + [
            e.get("url", "") for e in message.get("entities", []) + message.get("caption_entities", [])
        ] + [b.get("url", "") for row in message.get("reply_markup", {}).get("inline_keyboard", []) for b in row])
        reply_to = message.get("reply_to_message", {}).get("message_id")
        mapped = reply_to and self.db.one(
            "SELECT order_id FROM reply_map WHERE chat_id=? AND message_id=?", uid, reply_to)
        state = self.get_state(uid)

        if state and state["kind"] in ("bcast", "bsend"):
            self.core.require_owner(uid)
            self.set_state(uid, "bsend", 0, message["message_id"], str(message["message_id"]))
            await self.send(uid, "📣 Отправить это сообщение <b>всем пользователям</b>?",
                            keyboard([button("✅ Отправить", f"bcok:{message['message_id']}", "success"),
                                      button("✖️ Отмена", "no", "danger")]))
            return

        # Видео для проверки «Не пришло»
        is_video = kind in VIDEO_KINDS or (
            kind == "document" and message["document"].get("mime_type", "").startswith("video/"))
        if is_video:
            payment = self.core.awaiting_video(uid)
            if payment:
                await self.take_video(uid, message, payment)
                return

        order_id = mapped["order_id"] if mapped else (state["order_id"] if state else None)
        order = self.core.order(order_id) if order_id else None
        staff_writes = bool(order and self.core.role(uid) and
                            (order["user_id"] != uid or order["manager_id"] == uid))

        # Ссылка клиента на чек всегда идёт в пополнение, а не менеджеру в переписку.
        if parse_link(link_text) and not staff_writes:
            self.deposits.submit(uid, link_text)
            await self.send(uid, "🔎 Проверяю чек, это займёт до минуты…")
            return

        if state and state["kind"] in ("give", "confirm") and not mapped:
            amount = self.core.check_amount(uid, state["order_id"], text)
            order = self.core.order(state["order_id"])
            nonce = str(message["message_id"])
            self.set_state(uid, "confirm", order["id"], amount, nonce)
            await self.purge(order["id"], uid, "prompt")
            self.track(uid, message["message_id"], order["id"], "prompt")
            await self.send(uid,
                            f"Заявка #{order['id']}. Выдать <b>{money.fmt(amount)} {esc(order['currency'])}</b>?\n"
                            f"{esc(order['bank'])} · <code>{esc(order['phone'])}</code> · {esc(order['fio'])}",
                            keyboard([button("✅ Да, отправлено", f"ok:{nonce}", "success"),
                                      button("✖️ Отмена", f"no:{order['id']}", "danger")]),
                            order["id"], "prompt")
            return

        if order_id:
            media = message.get(kind) if source else None
            if isinstance(media, list):
                media = media[-1]  # у фото берём самый крупный размер
            await self.core.add_message(uid, order_id, body, kind, source, (media or {}).get("file_id"))
            if state and state["kind"] == "chat":
                self.set_state(uid, "chat", order_id)
            if kind == "text":  # медиа клиента не удаляем: это оригиналы доказательств
                self.track(uid, message["message_id"], order_id, "chat")
            await self.send(uid, "Отправлено ✓" + ("  (/cancel — выйти из переписки)" if state else ""),
                            order_id=order_id, tag="chat")
            return
        await self.send(uid, "Пришлите ссылку на чек или откройте приложение. Чтобы написать по заявке, "
                             "нажмите «Написать» под ней.", keyboard([self.app_button()]))

    async def take_video(self, uid, message, payment):
        order = self.core.order(payment["order_id"])
        delivered = 0
        for chat_id in self.staff_for(order):
            await self.send(chat_id, f"🎥 Видео по заявке <b>#{order['id']}</b>, выплата "
                                     f"{money.fmt(payment['amount'])}. Должен быть код "
                                     f"<code>{payment['video_code']}</code>", order_id=order["id"], tag="chat")
            try:
                copied = await self.api.call("copyMessage", chat_id=chat_id, from_chat_id=uid,
                                             message_id=message["message_id"])
                self.track(chat_id, copied["message_id"], order["id"], "chat")
                delivered += 1
            except TelegramError as exc:
                log.warning("video copy: %s", exc)
        if delivered:
            self.core.mark_video(payment["id"])
            kind = next(k for k in ("video", "video_note", "document") if k in message)
            self.core.add_system(order["id"], uid, f"Видео проверки, код {payment['video_code']}",
                                 kind, message[kind].get("file_id"))
            await self.send(uid, "🎥 Видео передано менеджеру. Ожидайте ответа.", order_id=order["id"], tag="info")
        else:
            await self.send(uid, "Не удалось передать видео, отправьте его ещё раз.")

    async def on_callback(self, query):
        uid = query["from"]["id"]
        self._touch(query["from"])
        action, _, raw = (query.get("data") or "").partition(":")
        answer, alert = None, False
        try:
            if action == "no":
                self.clear_state(uid)
                if raw.isdigit():
                    await self.purge(int(raw), uid, "prompt")
                    await self.restore_card(uid, int(raw))
                else:
                    await self.send(uid, "Отменено, ничего не изменилось.")
            elif action == "ok":
                state = self.get_state(uid)
                if not state or state["kind"] != "confirm" or state["nonce"] != raw:
                    raise Err("Подтверждение устарело")
                self.clear_state(uid)  # одноразово: двойное нажатие не создаст вторую выплату
                await self.core.give(uid, state["order_id"], money.fmt(state["amount"]).replace(" ", ""))
            elif action == "bal":
                await self.show_balance(uid)
            elif action == "help":
                await self.show_help(uid)
            elif action == "ref":
                await self.show_referral(uid)
            elif action == "bcok":
                state = self.get_state(uid)
                if not state or state["kind"] != "bsend" or state["nonce"] != raw:
                    raise Err("Подтверждение устарело")
                self.clear_state(uid)
                self.core.create_broadcast(uid, source=(uid, state["amount"]))
                await self.send(uid, "📣 Рассылка запущена. Пришлю отчёт, когда закончится.")
            elif action == "doneok":
                num, _, refund = raw.partition(":")
                if num.isdigit() and refund.isdigit():
                    await self.core.complete(uid, int(num), expect=refund)
            else:
                if not raw.isdigit():
                    return
                num = int(raw)
                if action == "acc":
                    await self.core.accept(uid, num)
                elif action == "rej":
                    order = self.core._managed(uid, num, ("new", "active"))
                    await self.send(uid, f"Отменить заявку #{num} и вернуть средства клиенту?",
                                    keyboard([button("Да, отменить", f"rejok:{order['id']}", "danger"),
                                              button("Нет", f"no:{num}")]), num, "prompt")
                elif action == "rejok":
                    await self.core.cancel(uid, num)
                elif action == "give":
                    order = self.core._managed(uid, num)
                    left = self.core.left(order)
                    if left <= 0:
                        raise Err("Вся сумма уже выдана")
                    self.set_state(uid, "give", num)
                    await self.send(uid, f"Введите сумму выплаты по заявке #{num} "
                                         f"(остаток <code>{money.fmt(left).replace(' ', '')}</code> "
                                         f"{esc(order['currency'])})",
                                    keyboard([button("✖️ Отмена", f"no:{num}")]), num, "prompt")
                elif action == "done":
                    order = self.core._managed(uid, num)
                    paid, refund = self.core.settlement(order)
                    cur = esc(order["currency"])
                    await self.send(uid,
                                    f"🏁 <b>Завершить заявку #{num}?</b>\n\n"
                                    f"Выплачено: <b>{money.fmt(paid)} {cur}</b> из "
                                    f"{money.fmt(order['total'])} {cur}\n"
                                    f"Вернётся клиенту на баланс: <b>{money.fmt(refund)} $</b>",
                                    keyboard([button("✅ Да, завершить", f"doneok:{num}:{refund}", "success"),
                                              button("Нет", f"no:{num}", "danger")]), num, "prompt")
                elif action in ("lost", "came"):
                    payment = self.core.payment(num)
                    if not payment or payment["status"] != "disputed":
                        raise Err("Спор по этой выплате уже закрыт")
                    order = self.core._managed(uid, payment["order_id"])
                    verdict = "пришли" if action == "came" else "НЕ пришли"
                    await self.send(uid, f"⚖️ Подтвердите: по выплате <b>{money.fmt(payment['amount'])} "
                                         f"{esc(order['currency'])}</b> деньги <b>{verdict}</b>?",
                                    keyboard([button("Да, подтверждаю", f"{action}ok:{num}",
                                                     "success" if action == "came" else "danger"),
                                              button("Нет", f"no:{order['id']}")]), order["id"], "prompt")
                elif action in ("lostok", "cameok"):
                    await self.core.resolve_dispute(uid, num, action == "cameok")
                elif action == "void":
                    await self.core.void_payment(uid, num)
                elif action == "chat":
                    self.core.chat_side(uid, num)
                    self.set_state(uid, "chat", num)
                    await self.send(uid, f"Напишите сообщение по заявке #{num}. /cancel — выйти",
                                    order_id=num, tag="chat")
                elif action == "got":
                    await self.core.received(uid, num)
                elif action == "nogot":
                    await self.core.not_received(uid, num)
                else:
                    return
            if action in ONE_SHOT and query.get("message"):
                await self.delete(query["message"]["chat"]["id"], [query["message"]["message_id"]])
        except Err as exc:
            answer, alert = str(exc)[:200], True
        finally:
            try:
                await self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                                    text=answer, show_alert=alert)
            except TelegramError as exc:
                log.info("answerCallbackQuery: %s", exc)

    # ---------- запуск ----------
    async def setup(self):
        url = self.core.config.url + "/"
        try:
            me = await self.api.call("getMe")
            self.core.set_setting("bot_username", me.get("username", ""))
        except TelegramError as exc:
            log.error("getMe: %s", exc)
        for method, params in (
            ("deleteWebhook", {}),
            ("setMyCommands", {"commands": [{"command": "start", "description": "Главное меню"},
                                            {"command": "balance", "description": "Баланс"},
                                            {"command": "cancel", "description": "Отменить ввод"}]}),
            ("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "Обменник",
                                                   "web_app": {"url": url}}}),
        ):
            try:
                await self.api.call(method, **params)
            except TelegramError as exc:
                log.error("%s: %s", method, exc)

    async def poll(self):
        await self.setup()
        offset = None
        log.info("Бот запущен")
        while True:
            try:
                updates = await self.api.call("getUpdates", _timeout=35, offset=offset, timeout=25,
                                              allowed_updates=["message", "callback_query"])
            except TelegramError as exc:
                log.error("getUpdates: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self.handle(update)
                except Exception:
                    log.exception("Ошибка обработки обновления")

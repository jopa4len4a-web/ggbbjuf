"""Диалоги бота и уведомления."""
import asyncio
import html
import logging

from . import money
from .cheques import parse_link
from .core import Err
from .telegram import TelegramError

log = logging.getLogger("bot")
STATE_TTL = 15 * 60
VIDEO_KINDS = ("video", "video_note")
MEDIA = {"photo": "фото", "video": "видео", "video_note": "видеосообщение", "voice": "голосовое",
         "document": "файл", "audio": "аудио"}
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
    async def send(self, chat_id, text, markup=None, order_id=None):
        try:
            message = await self.api.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                                          reply_markup=markup, link_preview_options={"is_disabled": True})
        except TelegramError as exc:
            log.warning("sendMessage %s: %s", chat_id, exc)
            return None
        if order_id and message:
            self.db.run("INSERT OR REPLACE INTO reply_map VALUES(?,?,?)",
                        chat_id, message["message_id"], order_id)
        return message

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
        lines = [
            f"<b>Заявка #{order['id']}</b> · {status}",
            f"Клиент: <a href=\"tg://user?id={client['id']}\">{esc(who)}</a> (ID <code>{client['id']}</code>)",
            f"Сумма: <b>{money.fmt(order['usd'])} $</b> × {money.fmt(order['rate'])} = "
            f"<b>{money.fmt(order['total'])} {esc(order['currency'])}</b>",
            f"Телефон: <code>{esc(order['phone'])}</code>",
            f"Банк: {esc(order['bank'])}",
            f"ФИО: <code>{esc(order['fio'])}</code>",
        ]
        if given:
            lines.append(f"Выдано: {money.fmt(given)} · подтверждено: {money.fmt(received)}")
        if order["manager_id"]:
            manager = self.core.user(order["manager_id"])
            lines.append("Менеджер: " + esc("@" + manager["username"] if manager and manager["username"]
                                            else order["manager_id"]))
        return "\n".join(lines)

    def staff_keyboard(self, order):
        oid = order["id"]
        if order["status"] == "new":
            return keyboard(
                [button("✅ Принять", f"acc:{oid}", "success"), button("Отклонить", f"rej:{oid}", "danger")],
                [self.app_button("Открыть в панели", oid)])
        if order["status"] != "active":
            return keyboard()
        pending = [p for p in self.core.payments(oid) if p["status"] in ("sent", "disputed")]
        rows = []
        if pending:
            rows.append([button(f"Отменить выплату {money.fmt(pending[0]['amount'])}",
                                f"void:{pending[0]['id']}")])
        else:
            rows.append([button("💸 Дать сумму", f"give:{oid}", "primary")])
        rows.append([button("✅ Завершить", f"done:{oid}", "success"),
                     button("💬 Написать", f"chat:{oid}")])
        rows.append([button("Отменить заявку", f"rej:{oid}", "danger")])
        rows.append([self.app_button("Открыть в панели", oid)])
        return keyboard(*rows)

    async def staff_card(self, chat_id, order, headline=""):
        text = (headline + "\n\n" if headline else "") + self.order_card(order)
        message = await self.send(chat_id, text, self.staff_keyboard(order), order["id"])
        if message and order["status"] == "new":
            self.db.run("INSERT OR REPLACE INTO order_notices VALUES(?,?,?)",
                        order["id"], chat_id, message["message_id"])

    # ---------- уведомления (вызываются из Core) ----------
    async def order_new(self, order_id):
        order = self.core.order(order_id)
        if order["created"] == order["updated"]:  # не повторяем при возврате в очередь
            await self.send(order["user_id"],
                            f"Заявка <b>#{order_id}</b> создана: {money.fmt(order['usd'])} $ → "
                            f"<b>{money.fmt(order['total'])} {esc(order['currency'])}</b>.\nЖдём менеджера.",
                            keyboard([self.app_button("Открыть заявку", order_id)]))
        staff = self.core.staff_ids()
        if not staff:
            log.warning("Нет администраторов: пусть @%s нажмёт /start", self.core.config.owner_username)
        for chat_id in staff:
            await self.staff_card(chat_id, order, "🔔 Новая заявка")

    async def _retire_notices(self, order):
        for row in self.db.all("SELECT * FROM order_notices WHERE order_id=?", order["id"]):
            if row["chat_id"] == order["manager_id"]:
                text = self.order_card(order)
            else:
                note = "взята другим менеджером" if order["status"] == "active" else "закрыта"
                text = f"Заявка #{order['id']} {note}."
            try:
                await self.api.call("editMessageText", chat_id=row["chat_id"], message_id=row["message_id"],
                                    text=text, parse_mode="HTML", reply_markup=keyboard())
            except TelegramError as exc:
                log.info("edit notice: %s", exc)
        self.db.run("DELETE FROM order_notices WHERE order_id=?", order["id"])

    async def order_accepted(self, order_id):
        order = self.core.order(order_id)
        await self._retire_notices(order)
        await self.staff_card(order["manager_id"], order, "Вы приняли заявку")
        await self.send(order["user_id"], f"✅ Заявка <b>#{order_id}</b> принята. Менеджер готовит выплату.",
                        keyboard([button("💬 Написать менеджеру", f"chat:{order_id}")],
                                 [self.app_button("Открыть заявку", order_id)]), order_id)

    def payment_keyboard(self, payment):
        return keyboard([button("✅ Пришло", f"got:{payment['id']}", "success"),
                         button("❌ Не пришло", f"nogot:{payment['id']}", "danger")],
                        [button("💬 Написать менеджеру", f"chat:{payment['order_id']}")])

    async def payment_sent(self, payment_id):
        payment = self.core.payment(payment_id)
        order = self.core.order(payment["order_id"])
        minutes = max(1, round((payment["deadline"] - payment["created"]) / 60))
        amount = f"{money.fmt(payment['amount'])} {esc(order['currency'])}"
        await self.send(order["user_id"],
                        f"💸 По заявке <b>#{order['id']}</b> отправлено <b>{amount}</b>\n"
                        f"{esc(order['bank'])}, <code>{esc(order['phone'])}</code>\n\n"
                        f"⏱ Проверьте поступление в течение <b>{minutes} мин</b> и нажмите кнопку.\n"
                        "Нажимайте «Пришло» только когда деньги действительно на счёте.",
                        self.payment_keyboard(payment), order["id"])
        await self.staff_card(order["manager_id"], order, f"Выплата {amount} отмечена, таймер {minutes} мин")

    async def payment_received(self, payment_id):
        payment = self.core.payment(payment_id)
        order = self.core.order(payment["order_id"])
        await self.send(order["user_id"], f"Спасибо, получение по заявке #{order['id']} подтверждено.")
        await self.staff_card(order["manager_id"], order,
                              f"🟢 Клиент подтвердил: <b>пришло {money.fmt(payment['amount'])}</b>")

    async def payment_disputed(self, payment_id):
        payment = self.core.payment(payment_id)
        order = self.core.order(payment["order_id"])
        await self.send(order["user_id"], self.video_text(payment["video_code"]),
                        keyboard([button("✅ Всё-таки пришло", f"got:{payment_id}", "success")],
                                 [button("💬 Написать менеджеру", f"chat:{order['id']}")]), order["id"])
        await self.staff_card(order["manager_id"], order,
                              f"🔴 Клиент: <b>НЕ пришло {money.fmt(payment['amount'])}</b>\n"
                              f"Код для видео: <code>{payment['video_code']}</code>")

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
        payment = self.core.payment(payment_id)
        order = self.core.order(payment["order_id"])
        await self.send(order["user_id"],
                        f"⏱ Время по заявке #{order['id']} вышло. Пожалуйста, ответьте: деньги пришли?",
                        self.payment_keyboard(payment), order["id"])
        await self.staff_card(order["manager_id"], order, "⏱ Таймер истёк, клиент не ответил")

    async def payment_void(self, payment_id):
        payment = self.core.payment(payment_id)
        order = self.core.order(payment["order_id"])
        await self.send(order["user_id"],
                        f"Выплата {money.fmt(payment['amount'])} {esc(order['currency'])} по заявке "
                        f"#{order['id']} отменена менеджером. Дождитесь новой.", order_id=order["id"])
        await self.staff_card(order["manager_id"], order, "Выплата отменена")

    async def order_done(self, order_id):
        order = self.core.order(order_id)
        await self.send(order["user_id"], f"🎉 Заявка <b>#{order_id}</b> завершена. Спасибо, что выбрали "
                                          "TwaragaSwap!", keyboard([self.app_button()]))
        await self.send(order["manager_id"], f"✅ Заявка #{order_id} завершена.")

    async def order_cancelled(self, order_id, actor):
        order = self.core.order(order_id)
        await self._retire_notices(order)
        await self.send(order["user_id"],
                        f"Заявка <b>#{order_id}</b> отменена. На баланс возвращено "
                        f"<b>{money.fmt(order['refunded'])} $</b>.", keyboard([self.app_button()]))
        for chat_id in {order["manager_id"], actor} - {None, order["user_id"]}:
            await self.send(chat_id, f"Заявка #{order_id} отменена, возврат {money.fmt(order['refunded'])} $.")

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
                await self.send(chat_id, title, order_id=oid)
                try:
                    copied = await self.api.call("copyMessage", chat_id=chat_id, from_chat_id=source[0],
                                                 message_id=source[1], reply_markup=reply)
                    self.db.run("INSERT OR REPLACE INTO reply_map VALUES(?,?,?)",
                                chat_id, copied["message_id"], oid)
                except TelegramError as exc:
                    log.warning("copyMessage: %s", exc)
            else:
                await self.send(chat_id, f"<b>{title}</b>\n{esc(msg['text'])}", reply, oid)

    # ---------- состояния ввода ----------
    def set_state(self, user_id, kind, order_id, amount=None, nonce=None):
        self.db.run("INSERT OR REPLACE INTO states VALUES(?,?,?,?,?,?)",
                    user_id, kind, order_id, amount, nonce, self.core.now() + STATE_TTL)

    def get_state(self, user_id):
        return self.db.one("SELECT * FROM states WHERE user_id=? AND expires>?", user_id, self.core.now())

    def clear_state(self, user_id):
        self.db.run("DELETE FROM states WHERE user_id=?", user_id)

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
        self._touch(message["from"])
        text = (message.get("text") or "").strip()
        try:
            if text.startswith("/start"):
                self.clear_state(uid)
                await self.welcome(uid)
            elif text == "/cancel":
                self.clear_state(uid)
                await self.send(uid, "Ввод отменён.")
            elif text == "/balance":
                await self.send(uid, f"Баланс: <b>{money.fmt(self.core.user(uid)['balance'])} $</b>",
                                keyboard([self.app_button()]))
            else:
                await self.free_input(uid, message, text)
        except Err as exc:
            await self.send(uid, esc(exc))

    async def welcome(self, uid):
        s = self.core.settings()
        ok, why = self.core.work_state()
        rate = f"1 $ = {money.fmt(int(s['rate']))} {esc(s['currency'])}" if int(s["rate"]) else "курс не задан"
        text = ("<b>TwaragaSwap</b>\nОбмен чеков CryptoBot и xRocket на перевод по номеру телефона.\n\n"
                f"Курс: <b>{rate}</b>\nМинимум: {money.fmt(int(s['min_usd']))} $\n"
                f"{'🟢 Принимаем заявки' if ok else '🔴 ' + esc(why)}\n\n"
                "Чтобы пополнить баланс, пришлите ссылку на чек прямо в этот чат.")
        if self.core.role(uid):
            text += "\n\n👤 Вы администратор: панель находится в приложении."
        await self.send(uid, text, keyboard([self.app_button()]))

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
            await self.send(uid,
                            f"Заявка #{order['id']}. Выдать <b>{money.fmt(amount)} {esc(order['currency'])}</b>?\n"
                            f"{esc(order['bank'])} · <code>{esc(order['phone'])}</code> · {esc(order['fio'])}",
                            keyboard([button("✅ Да, отправлено", f"ok:{nonce}", "success"),
                                      button("Отмена", "no", "danger")]))
            return

        if order_id:
            await self.core.add_message(uid, order_id, body, kind, source)
            if state and state["kind"] == "chat":
                self.set_state(uid, "chat", order_id)
            await self.send(uid, "Отправлено ✓" + ("  (/cancel — выйти из переписки)" if state else ""))
            return
        await self.send(uid, "Пришлите ссылку на чек или откройте приложение. Чтобы написать по заявке, "
                             "нажмите «Написать» под ней.", keyboard([self.app_button()]))

    async def take_video(self, uid, message, payment):
        order = self.core.order(payment["order_id"])
        delivered = 0
        for chat_id in self.staff_for(order):
            await self.send(chat_id, f"🎥 Видео по заявке <b>#{order['id']}</b>, выплата "
                                     f"{money.fmt(payment['amount'])}. Должен быть код "
                                     f"<code>{payment['video_code']}</code>", order_id=order["id"])
            try:
                await self.api.call("copyMessage", chat_id=chat_id, from_chat_id=uid,
                                    message_id=message["message_id"])
                delivered += 1
            except TelegramError as exc:
                log.warning("video copy: %s", exc)
        if delivered:
            self.core.mark_video(payment["id"])
            await self.send(uid, "🎥 Видео передано менеджеру. Ожидайте ответа.")
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
                await self.send(uid, "Отменено, ничего не отправлено.")
            elif action == "ok":
                state = self.get_state(uid)
                if not state or state["kind"] != "confirm" or state["nonce"] != raw:
                    raise Err("Подтверждение устарело")
                self.clear_state(uid)  # одноразово: двойное нажатие не создаст вторую выплату
                await self.core.give(uid, state["order_id"], money.fmt(state["amount"]).replace(" ", ""))
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
                                              button("Нет", "no")]))
                elif action == "rejok":
                    await self.core.cancel(uid, num)
                elif action == "give":
                    order = self.core._managed(uid, num)
                    left = order["total"] - self.core.sums(num)[0]
                    if left <= 0:
                        raise Err("Вся сумма уже выдана")
                    self.set_state(uid, "give", num)
                    await self.send(uid, f"Введите сумму выплаты по заявке #{num} "
                                         f"(остаток <code>{money.fmt(left).replace(' ', '')}</code> "
                                         f"{esc(order['currency'])}). /cancel — отмена")
                elif action == "done":
                    await self.core.complete(uid, num)
                elif action == "void":
                    await self.core.void_payment(uid, num)
                elif action == "chat":
                    self.core.chat_side(uid, num)
                    self.set_state(uid, "chat", num)
                    await self.send(uid, f"Напишите сообщение по заявке #{num}. /cancel — выйти")
                elif action == "got":
                    await self.core.received(uid, num)
                elif action == "nogot":
                    await self.core.not_received(uid, num)
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

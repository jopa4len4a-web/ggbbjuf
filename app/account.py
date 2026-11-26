"""Telegram-аккаунт (Telethon), который активирует чеки."""
import asyncio
import json
import logging
import os
import time

from .cheques import Result, parse_reply

log = logging.getLogger("account")


class AccountError(Exception):
    pass


class Account:
    REPLY_TIMEOUT = 25
    QUIET_AFTER_TIMEOUT = 90   # чтобы запоздавший ответ не приписался следующему чеку

    def __init__(self, config):
        self.config = config
        self.path = config.data_dir / "account.json"
        self.client = None
        self.me = None
        self.error = ""
        self._pending = None
        self._lock = asyncio.Lock()
        self._quiet_until = {}

    # ---------- сессия ----------
    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            if self.config.session_string and self.config.api_id:
                return {"api_id": self.config.api_id, "api_hash": self.config.api_hash,
                        "session": self.config.session_string}
            return None

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
        os.replace(tmp, self.path)

    def _make_client(self, api_id, api_hash, session=""):
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:
            raise AccountError("Библиотека telethon не установлена") from exc
        return TelegramClient(StringSession(session), int(api_id), api_hash,
                              device_model="TwaragaSwap", system_version="Server")

    @property
    def ready(self):
        return bool(self.client and self.me and self.client.is_connected())

    def status(self):
        return {"connected": self.ready, "name": self.me or "", "error": self.error,
                "pending": self._pending["step"] if self._pending else "",
                "has_api": bool(self.config.api_id and self.config.api_hash)}

    async def start(self):
        data = self._load()
        if not data:
            return
        try:
            client = self._make_client(data["api_id"], data["api_hash"], data["session"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise AccountError("Сессия аккаунта больше не действует, подключите заново")
            await self._adopt(client)
        except Exception as exc:
            self.error = str(exc)[:200]
            log.error("Аккаунт не подключён: %s", exc)

    async def _adopt(self, client):
        user = await client.get_me()
        if getattr(user, "bot", False):
            raise AccountError("Нужен обычный аккаунт, а не бот")
        self.client = client
        self.me = f"@{user.username}" if user.username else (user.first_name or str(user.id))
        self.error = ""

    # ---------- вход из админ-панели ----------
    async def send_code(self, api_id, api_hash, phone):
        api_id = str(api_id or self.config.api_id).strip()
        api_hash = str(api_hash or self.config.api_hash).strip()
        phone = str(phone or "").replace(" ", "")
        if not api_id.isdigit() or len(api_hash) != 32 or not phone:
            raise AccountError("Нужны api_id, api_hash (32 символа) и номер телефона")
        if self._pending:
            await self._pending["client"].disconnect()
            self._pending = None
        client = self._make_client(api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as exc:
            await client.disconnect()
            raise AccountError(f"Telegram отклонил запрос: {exc}") from exc
        self._pending = {"client": client, "api_id": api_id, "api_hash": api_hash, "phone": phone,
                         "hash": sent.phone_code_hash, "step": "code"}

    async def sign_in(self, code="", password=""):
        pending = self._pending
        if not pending:
            raise AccountError("Сначала запросите код")
        client = pending["client"]
        try:
            if pending["step"] == "password":
                await client.sign_in(password=password)
            else:
                await client.sign_in(pending["phone"], str(code).strip(),
                                     phone_code_hash=pending["hash"])
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                pending["step"] = "password"
                return "password"
            raise AccountError(f"Не получилось войти: {exc}") from exc
        old = self.client
        await self._adopt(client)
        self._save({"api_id": pending["api_id"], "api_hash": pending["api_hash"],
                    "session": client.session.save()})
        self._pending = None
        if old and old is not client:
            await old.disconnect()
        return "done"

    async def logout(self):
        async with self._lock:
            if self.client:
                try:
                    await self.client.log_out()
                except Exception:
                    log.exception("logout")
            self.client = self.me = None
            self.path.unlink(missing_ok=True)

    async def stop(self):
        for client in (self.client, self._pending and self._pending["client"]):
            if client:
                await client.disconnect()

    # ---------- активация ----------
    async def activate(self, bot_username: str, code: str, on_sent) -> Result:
        """on_sent() вызывается перед отправкой: после этого повторять активацию вслепую нельзя."""
        async with self._lock:
            if not self.ready:
                raise AccountError("Аккаунт не подключён")
            wait = self._quiet_until.get(bot_username, 0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            entity = await self.client.get_entity(bot_username)
            if not getattr(entity, "bot", False):
                raise AccountError(f"@{bot_username} не является ботом")
            on_sent()
            sent = await self.client.send_message(entity, f"/start {code}")
            loop = asyncio.get_running_loop()
            end = loop.time() + self.REPLY_TIMEOUT
            last = ""
            while loop.time() < end:
                await asyncio.sleep(1.5)
                messages = await self.client.get_messages(entity, min_id=sent.id, limit=10)
                for message in sorted(messages, key=lambda m: m.id):
                    if message.out:
                        continue
                    text = message.raw_text or ""
                    result = parse_reply(text)
                    if result:
                        return result
                    last = text or last
            self._quiet_until[bot_username] = time.monotonic() + self.QUIET_AFTER_TIMEOUT
            return Result("review", note="Бот не прислал понятного ответа, нужна проверка",
                          reply=last[:600])

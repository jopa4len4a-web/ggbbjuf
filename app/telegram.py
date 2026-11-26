"""Минимальный клиент Bot API на requests (запросы выполняются в потоках)."""
import asyncio
import logging

import requests

log = logging.getLogger("telegram")


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=0):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class BotAPI:
    def __init__(self, token):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._token = token

    def _post(self, method, params, timeout):
        try:
            response = requests.post(self._base + method, json=params, timeout=timeout)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Текст исключения содержит URL с токеном — вырезаем.
            raise TelegramError(0, str(exc).replace(self._token, "***")) from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", 0), data.get("description", ""),
                                data.get("parameters", {}).get("retry_after", 0))
        return data["result"]

    async def call(self, method, _timeout=20, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._post, method, params, _timeout)
            except TelegramError as exc:
                if exc.code == 429 and attempt < 2:
                    await asyncio.sleep(min(exc.retry_after or 1, 30))
                    continue
                raise

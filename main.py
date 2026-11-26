"""TwaragaSwap: бот + мини-приложение в одном процессе."""
import asyncio
import contextlib
import logging
import sys

import uvicorn

from app import config as config_module
from app.account import Account
from app.bot import Bot
from app.core import Core
from app.db import DB
from app.deposits import Deposits
from app.telegram import BotAPI
from app.web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def timers(core):
    while True:
        try:
            await core.expire_timers()
        except Exception:
            log.exception("timers")
        await asyncio.sleep(5)


def build():
    cfg = config_module.load()
    if not cfg.bot_token:
        sys.exit("Не задан BOT_TOKEN")
    core = Core(DB(cfg.data_dir / "twaraga.db"), cfg)
    account = Account(cfg)
    deposits = Deposits(core, account)
    bot = Bot(BotAPI(cfg.bot_token), core, deposits)
    core.notifier = bot

    @contextlib.asynccontextmanager
    async def lifespan(app):
        deposits.recover()

        async def cheque_service():  # вход в аккаунт не задерживает запуск веб-сервера
            await account.start()
            await deposits.worker()

        tasks = [asyncio.create_task(job) for job in (bot.poll(), cheque_service(), timers(core))]
        log.info("Mini App: %s  порт: %s  данные: %s", cfg.url, cfg.port, cfg.data_dir)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await account.stop()

    return cfg, create_app(core, deposits, account, lifespan)


if __name__ == "__main__":
    cfg, app = build()
    uvicorn.run(app, host="0.0.0.0", port=cfg.port, access_log=False)

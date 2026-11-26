import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from config import BOT_TOKEN, PORT, DOMAIN
from bot.handlers import router
from db.database import init_db

logging.basicConfig(level=logging.INFO)

async def on_startup(bot: Bot):
    await init_db()
    await bot.set_webhook(f"{DOMAIN}/webhook/{BOT_TOKEN}")
    logging.info("Webhook set")

def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=f"/webhook/{BOT_TOKEN}")
    setup_application(app, dp, bot=bot)

    dp.startup.register(on_startup)

    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()

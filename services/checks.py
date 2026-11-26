import asyncio
import logging
import aiohttp
from config import CRYPTOBOT_TOKEN, XROCKET_API_KEY
from db.database import set_check_request_status, add_balance

log = logging.getLogger(__name__)

CRYPTOBOT_URL = "https://pay.crypt.bot/api"
XROCKET_URL = "https://pay.xrocket.tg/api"

async def verify_cryptobot(cheque_id: str) -> bool:
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{CRYPTOBOT_URL}/getChecks", headers=headers,
                             params={"check_ids": cheque_id}, timeout=15) as r:
                data = await r.json()
                if data.get("ok"):
                    for c in data["result"]:
                        if c["status"] == "activated":
                            return True
    except Exception as e:
        log.warning(f"CryptoBot verify failed: {e}")
    return False

async def verify_xrocket(cheque_id: str) -> bool:
    headers = {"Authorization": f"Bearer {XROCKET_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{XROCKET_URL}/cheque/{cheque_id}", headers=headers, timeout=15) as r:
                data = await r.json()
                return data.get("status") == "paid"
    except Exception as e:
        log.warning(f"xRocket verify failed: {e}")
    return False

async def background_verify(bot, req_id: int, user_id: int, service: str,
                            cheque_id: str, amount: float):
    """Фоновая проверка чека — не блокирует бота."""
    if service == "cryptobot":
        ok = await verify_cryptobot(cheque_id)
    elif service == "xrocket":
        ok = await verify_xrocket(cheque_id)
    else:
        ok = False

    if ok:
        await set_check_request_status(req_id, "verified")
        await add_balance(user_id, amount)
        try:
            await bot.send_message(user_id,
                f"✅ Ваш чек на {amount:.2f} ₽ подтверждён и зачислен на баланс.")
        except Exception:
            pass
    else:
        await set_check_request_status(req_id, "failed")
        try:
            await bot.send_message(user_id,
                f"⚠️ Чек на {amount:.2f} ₽ не прошёл проверку. Свяжитесь с менеджером.")
        except Exception:
            pass

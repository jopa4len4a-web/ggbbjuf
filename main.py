import os, json, hmac, hashlib, time, sqlite3, asyncio, base64
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", "3000"))
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "ownerparadoxy").lstrip("@").lower()
MIN_ORDER = float(os.getenv("MIN_ORDER_USD", "1"))
MAX_ORDER = float(os.getenv("MAX_ORDER_USD", "5000"))
DB_PATH = os.getenv("DB_PATH", "exchange.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN environment variable")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app = FastAPI(title="Exchange Bot API")
app.mount("/static", StaticFiles(directory="static"), name="static")
broadcast_drafts = set()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins(tg_id INTEGER PRIMARY KEY, role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL,
          amount_usd REAL NOT NULL, quote REAL NOT NULL, status TEXT NOT NULL,
          created_at INTEGER NOT NULL, manager_id INTEGER,
          details TEXT NOT NULL DEFAULT '', manager_amount REAL
        );
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT, created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS demo_checks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL, amount REAL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS demo_check_messages(
          message_id INTEGER PRIMARY KEY, manager_id INTEGER NOT NULL,
          check_id INTEGER NOT NULL
        );
        """)
        # Миграция существующей базы без потери заявок.
        cols = {r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()}
        if "details" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN details TEXT NOT NULL DEFAULT ''")
        if "manager_amount" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN manager_amount REAL")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('rate','97.0')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('is_open','1')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('work_hours','Ежедневно, 10:00–22:00 (по местному времени)')")
        for aid in ADMIN_IDS:
            c.execute("INSERT OR IGNORE INTO admins(tg_id,role) VALUES(?, 'owner')", (aid,))

def is_admin(tg_id: int) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE tg_id=?", (tg_id,)).fetchone() is not None

def is_owner(tg_id: int) -> bool:
    with db() as c:
        r = c.execute("SELECT role FROM admins WHERE tg_id=?", (tg_id,)).fetchone()
        return bool(r and r["role"] == "owner")

def get_setting(key: str, default: str = ""):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def get_rate():
    return float(get_setting("rate", "97.0"))

def is_exchange_open():
    return get_setting("is_open", "1") == "1"

def audit(actor, action):
    with db() as c:
        c.execute("INSERT INTO audit(actor_id,action,created_at) VALUES(?,?,?)",
                  (actor, action[:500], int(time.time())))

def save_user(u):
    with db() as c:
        c.execute("""INSERT INTO users(tg_id,username,first_name,created_at)
        VALUES(?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET
        username=excluded.username, first_name=excluded.first_name""",
        (u.id, u.username or "", u.first_name or "", int(time.time())))
        # Bootstrap owner by the configured Telegram username after their first /start.
        if u.username and u.username.lower() == OWNER_USERNAME:
            c.execute("INSERT OR REPLACE INTO admins(tg_id,role) VALUES(?, 'owner')", (u.id,))

def validate_init_data(init_data: str):
    if not init_data:
        raise HTTPException(401, "Telegram initData missing")
    vals = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = vals.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Invalid initData")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Invalid Telegram signature")
    auth_date = int(vals.get("auth_date", "0"))
    if abs(int(time.time()) - auth_date) > 86400:
        raise HTTPException(401, "Expired initData")
    user = json.loads(vals.get("user", "{}"))
    if not user.get("id"):
        raise HTTPException(401, "Telegram user missing")
    return user

async def get_user(request: Request):
    return validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.get("/api/me")
async def me(request: Request):
    u = await get_user(request)
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE tg_id=?", (u["id"],)).fetchone()
        if not row:
            c.execute("INSERT INTO users(tg_id,username,first_name,created_at) VALUES(?,?,?,?)",
                      (u["id"], u.get("username",""), u.get("first_name",""), int(time.time())))
    return {"id": u["id"], "username": u.get("username",""),
            "rate": get_rate(), "min": MIN_ORDER, "max": MAX_ORDER,
            "is_admin": is_admin(int(u["id"])),
            "is_owner": is_owner(int(u["id"])),
            "is_open": is_exchange_open(),
            "work_hours": get_setting("work_hours", "График не задан")}

@app.post("/api/orders")
async def create_order(request: Request):
    u = await get_user(request)
    if not is_exchange_open():
        raise HTTPException(403, "Обменник сейчас закрыт. Посмотрите график работы и попробуйте позже.")
    body = await request.json()
    try:
        amount = float(body.get("amount_usd"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid amount")
    if not (MIN_ORDER <= amount <= MAX_ORDER):
        raise HTTPException(400, f"Сумма должна быть от {MIN_ORDER:g} до {MAX_ORDER:g} USD")
    rate = get_rate()
    details = str(body.get("details", ""))[:1000].strip()
    quote = round(amount * rate, 2)
    with db() as c:
        cur = c.execute("INSERT INTO orders(tg_id,amount_usd,quote,status,created_at,details) VALUES(?,?,?,?,?,?)",
                        (u["id"], amount, quote, "new", int(time.time()), details))
        oid = cur.lastrowid
        managers = [int(r["tg_id"]) for r in c.execute("SELECT tg_id FROM admins").fetchall()]
    audit(int(u["id"]), f"Создана демо-заявка #{oid}")
    for aid in managers:
        try:
            await bot.send_message(aid, f"Новая демо-заявка <b>#{oid}</b>\nПользователь: <code>{u['id']}</code>\nСумма: {amount:g} USD\nРасчёт: {quote:g}\nКомментарий: {details or '—'}")
        except Exception:
            pass
    return {"ok": True, "order_id": oid, "amount_usd": amount, "quote": quote,
            "notice": "Демонстрационная заявка создана. Реальная выплата не производится."}

@app.get("/api/my-orders")
async def my_orders(request: Request):
    u = await get_user(request)
    with db() as c:
        rows = c.execute("SELECT id,amount_usd,quote,status,created_at,details,manager_amount FROM orders WHERE tg_id=? ORDER BY id DESC LIMIT 50", (u["id"],)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/orders")
async def orders(request: Request):
    u = await get_user(request)
    if not is_admin(int(u["id"])):
        raise HTTPException(403, "Forbidden")
    with db() as c:
        rows = c.execute("""SELECT o.*,u.username FROM orders o LEFT JOIN users u
        ON u.tg_id=o.tg_id ORDER BY o.id DESC LIMIT 100""").fetchall()
    return [dict(r) for r in rows]

@app.post("/api/rate")
async def set_rate(request: Request):
    u = await get_user(request)
    if not is_owner(int(u["id"])):
        raise HTTPException(403, "Owner only")
    body = await request.json()
    try:
        rate = float(body.get("rate"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid rate")
    if not (0.01 <= rate <= 100000):
        raise HTTPException(400, "Rate out of safe bounds")
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='rate'", (str(rate),))
    audit(int(u["id"]), f"Изменён курс: {rate}")
    return {"ok": True, "rate": rate}


@app.post("/api/work-hours")
async def set_work_hours(request: Request):
    u = await get_user(request)
    if not is_admin(int(u["id"])):
        raise HTTPException(403, "Forbidden")
    body = await request.json()
    status = body.get("is_open")
    schedule = str(body.get("work_hours", ""))[:200]
    if status is not None and not isinstance(status, bool):
        raise HTTPException(400, "Invalid open status")
    if status is None and not schedule.strip():
        raise HTTPException(400, "Schedule is empty")
    with db() as c:
        if status is not None:
            c.execute("UPDATE settings SET value=? WHERE key='is_open'", ("1" if status else "0",))
        if schedule.strip():
            c.execute("UPDATE settings SET value=? WHERE key='work_hours'", (schedule.strip(),))
    audit(int(u["id"]), f"Изменены часы работы: is_open={status}, schedule={schedule}")
    return {"ok": True, "is_open": is_exchange_open(),
            "work_hours": get_setting("work_hours", "")}

@app.post("/api/order-status")
async def order_status(request: Request):
    u = await get_user(request)
    if not is_admin(int(u["id"])):
        raise HTTPException(403, "Forbidden")
    body = await request.json()
    oid, status = body.get("order_id"), body.get("status")
    if status not in {"accepted", "rejected", "completed"}:
        raise HTTPException(400, "Invalid status")
    amount = body.get("manager_amount")
    if amount is not None:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise HTTPException(400, "Некорректная тестовая сумма")
        if not (0 < amount <= 1_000_000):
            raise HTTPException(400, "Тестовая сумма вне допустимого диапазона")
    with db() as c:
        row = c.execute("SELECT tg_id FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "Order not found")
        if amount is None:
            c.execute("UPDATE orders SET status=?,manager_id=? WHERE id=?", (status, u["id"], oid))
        else:
            c.execute("UPDATE orders SET status=?,manager_id=?,manager_amount=? WHERE id=?", (status, u["id"], amount, oid))
    audit(int(u["id"]), f"Заявка #{oid}: {status}; сумма={amount}")
    try:
        msg = f"Статус демо-заявки #{oid}: <b>{status}</b>"
        if amount is not None:
            msg += f"\nТестовая сумма, указанная менеджером: <b>{amount:g}</b>"
        msg += "\nЭто демонстрационные данные, выплата не производится."
        await bot.send_message(row["tg_id"], msg)
    except Exception:
        pass
    return {"ok": True}

@app.post("/api/broadcast")
async def broadcast(request: Request):
    u = await get_user(request)
    if not is_admin(int(u["id"])):
        raise HTTPException(403, "Forbidden")
    body = await request.json()
    text = str(body.get("text", ""))[:4000]
    photo = body.get("photo_file_id")
    photo_bytes = None
    if body.get("photo_base64"):
        try:
            photo_bytes = base64.b64decode(body["photo_base64"], validate=True)
        except Exception:
            raise HTTPException(400, "Invalid image")
        if len(photo_bytes) > 8 * 1024 * 1024:
            raise HTTPException(400, "Image too large")
    button_text = str(body.get("button_text", ""))[:64]
    button_url = str(body.get("button_url", ""))[:500]
    if not text and not photo and not photo_bytes:
        raise HTTPException(400, "Empty broadcast")
    if button_url and not button_url.startswith(("https://", "http://")):
        raise HTTPException(400, "Invalid URL")
    with db() as c:
        ids = [r["tg_id"] for r in c.execute("SELECT tg_id FROM users").fetchall()]
    markup = None
    if button_text and button_url:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]])
    sent = failed = 0
    for uid in ids:
        try:
            if photo_bytes:
                upload = BufferedInputFile(photo_bytes, filename=str(body.get("photo_name") or "broadcast.jpg"))
                await bot.send_photo(uid, upload, caption=text or None, reply_markup=markup)
            elif photo:
                await bot.send_photo(uid, photo, caption=text or None, reply_markup=markup)
            else:
                await bot.send_message(uid, text, reply_markup=markup)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    audit(int(u["id"]), f"Рассылка: отправлено {sent}, ошибок {failed}")
    return {"ok": True, "sent": sent, "failed": failed, "total": len(ids)}

@dp.message(CommandStart())
async def start(message: Message):
    save_user(message.from_user)
    if WEBAPP_URL:
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Открыть обменник", web_app=WebAppInfo(url=WEBAPP_URL))]], resize_keyboard=True)
        await message.answer("Добро пожаловать. Это демонстрационная версия обменника.", reply_markup=kb)
    else:
        await message.answer("Бот запущен. Укажите WEBAPP_URL для подключения Mini App.")

@dp.message(Command("addadmin"))
async def add_admin(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await message.answer("Недостаточно прав.")
    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.answer("Использование: /addadmin @username или /addadmin TELEGRAM_ID")
    target = parts[1].lstrip("@")
    with db() as c:
        if target.isdigit():
            uid = int(target)
        else:
            row = c.execute("SELECT tg_id FROM users WHERE lower(username)=lower(?)", (target,)).fetchone()
            if not row:
                return await message.answer("Пользователь не найден. Он должен сначала запустить бота командой /start.")
            uid = int(row["tg_id"])
        c.execute("INSERT OR REPLACE INTO admins(tg_id,role) VALUES(?, 'manager')", (uid,))
    audit(message.from_user.id, f"Добавлен менеджер {uid}")
    await message.answer(f"Менеджер {uid} добавлен.")

@dp.message(Command("deladmin"))
async def del_admin(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await message.answer("Недостаточно прав.")
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Использование: /deladmin TELEGRAM_ID")
    if not parts[1].isdigit():
        return await message.answer("Для удаления используйте числовой Telegram ID.")
    uid = int(parts[1])
    with db() as c:
        c.execute("DELETE FROM admins WHERE tg_id=? AND role!='owner'", (uid,))
    audit(message.from_user.id, f"Удалён менеджер {uid}")
    await message.answer(f"Менеджер {uid} удалён.")

@dp.message(Command("broadcast"))
async def broadcast_start(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return await message.answer("Недостаточно прав.")
    broadcast_drafts.add(message.from_user.id)
    await message.answer("Пришлите текст или фото с подписью для рассылки. Для отмены: /cancel")

@dp.message(Command("cancel"))
async def cancel(message: Message):
    if message.from_user:
        broadcast_drafts.discard(message.from_user.id)
    await message.answer("Отменено.")

@dp.message(Command("demo_check"))
async def demo_check(message: Message):
    """Безопасный тестовый сценарий: передаёт текст менеджерам, без проверки реального чека."""
    if not message.from_user:
        return
    payload = (message.text or "").partition(" ")[2].strip()
    if not payload:
        return await message.answer(
            "Для теста отправьте: /demo_check описание_тестового_чека\n"
            "Не отправляйте реальные платёжные чеки, банковские данные или коды."
        )
    with db() as c:
        cur = c.execute(
            "INSERT INTO demo_checks(tg_id,payload,status,created_at) VALUES(?,?,?,?)",
            (message.from_user.id, payload[:1500], "waiting_manager", int(time.time()))
        )
        check_id = cur.lastrowid
        managers = [int(r["tg_id"]) for r in c.execute("SELECT tg_id FROM admins").fetchall()]

    delivered = 0
    for manager_id in managers:
        try:
            sent = await bot.send_message(
                manager_id,
                f"ТЕСТОВЫЙ ЧЕК #{check_id}\n"
                f"От пользователя: <code>{message.from_user.id}</code>\n"
                f"Содержимое: {payload[:1500]}\n\n"
                "Ответьте на это сообщение только числом — тестовой суммой чека. "
                "Это не подтверждает реальный платёж и не запускает выплату."
            )
            with db() as c:
                c.execute(
                    "INSERT OR REPLACE INTO demo_check_messages(message_id,manager_id,check_id) VALUES(?,?,?)",
                    (sent.message_id, manager_id, check_id)
                )
            delivered += 1
        except Exception:
            pass

    if delivered:
        await message.answer(
            f"Тестовый чек #{check_id} передан менеджерам. "
            "После ответа менеджера тестовая сумма появится в этом чате."
        )
    else:
        with db() as c:
            c.execute("UPDATE demo_checks SET status='manual_review' WHERE id=?", (check_id,))
        await message.answer(
            f"Тестовая заявка #{check_id} не доставлена менеджерам. "
            "Она отмечена для ручного рассмотрения. Автоматической выплаты нет."
        )
    audit(message.from_user.id, f"Создан тестовый чек #{check_id}")


@dp.message(F.reply_to_message, F.text)
async def manager_demo_check_reply(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    replied_id = message.reply_to_message.message_id
    with db() as c:
        mapping = c.execute(
            "SELECT check_id FROM demo_check_messages WHERE message_id=? AND manager_id=?",
            (replied_id, message.from_user.id)
        ).fetchone()
    if not mapping:
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        amount = float(raw)
    except ValueError:
        return await message.reply("Для тестового сценария ответьте одним числом, например: 12.50")
    if amount <= 0 or amount > 1_000_000:
        return await message.reply("Укажите положительную тестовую сумму.")
    check_id = int(mapping["check_id"])
    with db() as c:
        row = c.execute("SELECT tg_id FROM demo_checks WHERE id=?", (check_id,)).fetchone()
        if not row:
            return await message.reply("Тестовая заявка не найдена.")
        c.execute(
            "UPDATE demo_checks SET amount=?,status='amount_entered' WHERE id=?",
            (amount, check_id)
        )
    audit(message.from_user.id, f"Менеджер внёс тестовую сумму для чека #{check_id}: {amount}")
    try:
        await bot.send_message(
            int(row["tg_id"]),
            f"По тестовой заявке #{check_id} менеджер указал сумму: <b>{amount:g}</b>.\n"
            "Это демонстрационная отметка, не подтверждение платежа и не выплата."
        )
    except Exception:
        pass
    await message.reply(f"Тестовая сумма {amount:g} сохранена для заявки #{check_id}.")


@dp.message(F.from_user)
async def broadcast_content(message: Message):
    uid = message.from_user.id
    if uid not in broadcast_drafts or not is_admin(uid):
        return
    broadcast_drafts.discard(uid)
    users = []
    with db() as c:
        users = [r["tg_id"] for r in c.execute("SELECT tg_id FROM users").fetchall()]
    sent = failed = 0
    for target in users:
        try:
            if message.photo:
                await bot.send_photo(target, message.photo[-1].file_id, caption=message.caption or "")
            elif message.text:
                await bot.send_message(target, message.text)
            else:
                continue
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    audit(uid, f"Рассылка через Telegram: {sent} отправлено, {failed} ошибок")
    await message.answer(f"Рассылка завершена. Отправлено: {sent}, ошибок: {failed}.")

async def run_web():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    init_db()
    await asyncio.gather(dp.start_polling(bot), run_web())

if __name__ == "__main__":
    asyncio.run(main())

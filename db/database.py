import aiosqlite
from datetime import datetime

DB_PATH = "twaraga.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                phone TEXT,
                bank TEXT,
                full_name TEXT,
                balance REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount_usd REAL,
                amount_rub REAL,
                status TEXT DEFAULT 'pending',
                manager_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                timer_expires TIMESTAMP
            )
        """)
        # Аккаунты для приёма чеков (добавляет менеджер)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS check_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT,           -- 'cryptobot' или 'xrocket'
                account TEXT,           -- @username / адрес / ID
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Запросы на пополнение чеком (после ответа менеджера суммой)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS check_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                manager_id INTEGER,
                manager_msg_id INTEGER,
                user_msg_id INTEGER,
                service TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending',  -- pending / verified / failed
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)
        """)
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rate', '90')")
        await db.commit()

# --- базовые ---
async def get_rate():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key='rate'") as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 90.0

async def set_rate(rate: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET value=? WHERE key='rate'", (str(rate),))
        await db.commit()

async def add_admin(user_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?,?)", (user_id, username))
        await db.commit()

async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone() is not None

# --- заявки обмена ---
async def create_order(user_id: int, amount_usd: float, amount_rub: float):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (user_id, amount_usd, amount_rub) VALUES (?,?,?)",
            (user_id, amount_usd, amount_rub)
        )
        await db.commit()
        return cur.lastrowid

async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM orders WHERE id=?", (order_id,)) as cur:
            return await cur.fetchone()

async def update_order_status(order_id: int, status: str, manager_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if manager_id:
            await db.execute("UPDATE orders SET status=?, manager_id=? WHERE id=?", (status, manager_id, order_id))
        else:
            await db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        await db.commit()

# --- аккаунты для чеков ---
async def add_check_account(service: str, account: str, added_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO check_accounts (service, account, added_by) VALUES (?,?,?)",
            (service, account, added_by)
        )
        await db.commit()

async def list_check_accounts():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, service, account FROM check_accounts ORDER BY id DESC") as cur:
            return await cur.fetchall()

async def delete_check_account(acc_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM check_accounts WHERE id=?", (acc_id,))
        await db.commit()

# --- запросы на пополнение ---
async def create_check_request(user_id, manager_id, manager_msg_id, user_msg_id, service, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO check_requests
               (user_id, manager_id, manager_msg_id, user_msg_id, service, amount)
               VALUES (?,?,?,?,?,?)""",
            (user_id, manager_id, manager_msg_id, user_msg_id, service, amount)
        )
        await db.commit()
        return cur.lastrowid

async def get_check_request_by_manager_msg(manager_msg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM check_requests WHERE manager_msg_id=? ORDER BY id DESC LIMIT 1",
            (manager_msg_id,)
        ) as cur:
            return await cur.fetchone()

async def set_check_request_status(req_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE check_requests SET status=?, verified_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, req_id)
        )
        await db.commit()

async def add_balance(user_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
            (user_id, amount, amount)
        )
        await db.commit()

async def get_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0

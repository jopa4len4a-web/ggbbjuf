"""SQLite. Все обращения идут из одного asyncio-потока, поэтому блок tx() атомарен."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, name TEXT,
  balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
  blocked INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS admins(
  username TEXT PRIMARY KEY, user_id INTEGER UNIQUE,
  is_owner INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deposits(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, provider TEXT NOT NULL, code TEXT NOT NULL,
  status TEXT NOT NULL, usd INTEGER, asset TEXT, asset_amount TEXT, note TEXT, reply TEXT,
  created INTEGER NOT NULL, updated INTEGER NOT NULL, UNIQUE(provider, code));
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, usd INTEGER NOT NULL, rate INTEGER NOT NULL,
  total INTEGER NOT NULL, currency TEXT NOT NULL, phone TEXT NOT NULL, bank TEXT NOT NULL,
  fio TEXT NOT NULL, status TEXT NOT NULL, manager_id INTEGER, request_key TEXT NOT NULL,
  refunded INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, updated INTEGER NOT NULL,
  UNIQUE(user_id, request_key));
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL,
  deadline INTEGER NOT NULL, expired_notified INTEGER NOT NULL DEFAULT 0, video_code TEXT,
  video_received INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
  from_staff INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created INTEGER NOT NULL,
  file_id TEXT);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
  actor_id INTEGER, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deal_msgs(
  chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, order_id INTEGER NOT NULL, tag TEXT NOT NULL,
  PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS reply_map(
  chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, order_id INTEGER NOT NULL,
  PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS referrals(
  user_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ref_earnings(
  order_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, amount INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS broadcasts(
  id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, text TEXT, from_chat INTEGER, message_id INTEGER,
  button INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, last_user INTEGER NOT NULL DEFAULT 0,
  max_user INTEGER NOT NULL, total INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS states(
  user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, order_id INTEGER NOT NULL,
  amount INTEGER, nonce TEXT, expires INTEGER NOT NULL);
"""


class DB:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # Обновление базы, созданной прошлой версией.
        if "file_id" not in [r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")]:
            self.conn.execute("ALTER TABLE messages ADD COLUMN file_id TEXT")

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def run(self, sql, *args):
        return self.conn.execute(sql, args)

    def one(self, sql, *args):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

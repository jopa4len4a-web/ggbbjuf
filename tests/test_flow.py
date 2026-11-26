import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

from app import money
from app.account import Account
from app.bot import Bot
from app.cheques import Result, parse_link, parse_reply
from app.config import Config, webapp_url
from app.core import Core, Err
from app.db import DB
from app.deposits import Deposits
from app.web import create_app

TOKEN = "123:TEST"
OWNER, ADMIN, CLIENT, OTHER = 1, 2, 3, 4


class FakeAPI:
    def __init__(self):
        self.calls, self.mid = [], 100

    async def call(self, method, _timeout=0, **params):
        self.mid += 1
        self.calls.append((method, params))
        return {"message_id": self.mid}

    def texts(self, chat):
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage" and p["chat_id"] == chat]


class FakeAccount:
    ready = True
    result = Result("ok", usd=10000, asset="USDT", amount="100")
    boom = None

    async def activate(self, bot, code, on_sent):
        if self.boom == "before":
            raise RuntimeError("no net")
        on_sent()
        if self.boom == "after":
            raise RuntimeError("lost")
        return Result(**self.result.__dict__)

    def status(self):
        return {"connected": True}


class Env:
    def __init__(self, tmp_path):
        self.t = 1_800_000_000
        cfg = Config(TOKEN, "https://x.test", 3000, tmp_path, "ownerparadoxy", 0, 0, "", "")
        self.core = Core(DB(":memory:"), cfg, clock=lambda: self.t)
        self.account = FakeAccount()
        self.dep = Deposits(self.core, self.account)
        self.api = FakeAPI()
        self.bot = Bot(self.api, self.core, self.dep)
        self.core.notifier = self.bot
        for uid, name in ((OWNER, "OwnerParadoxy"), (CLIENT, "client"), (OTHER, "other")):
            self.core.touch_user(uid, name, name)
        self.core.add_admin(OWNER, "@Manager")
        self.core.touch_user(ADMIN, "manager", "m")
        self.core.update_settings(OWNER, {"rate": "90"})
        self.n = 0

    def fund(self, usd=10000):
        with self.core.db.tx():
            self.core._move(CLIENT, usd, "test")

    async def order(self, usd="100", **kw):
        self.n += 1
        data = dict(phone="+79000000000", bank="Банк", fio="Иванов Иван", rate_seen="9000",
                    request_key=f"request-{self.n:04d}")
        data.update(kw)
        return await self.core.create_order(CLIENT, usd, **data)

    @property
    def balance(self):
        return self.core.user(CLIENT)["balance"]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def run(coro):
    return asyncio.run(coro)


def msg(uid, text=None, mid=1, **extra):
    names = {OWNER: "OwnerParadoxy", ADMIN: "manager", CLIENT: "client", OTHER: "other"}
    body = {"message_id": mid, "chat": {"type": "private", "id": uid},
            "from": {"id": uid, "username": names[uid], "first_name": "n"}, **extra}
    if text is not None:
        body["text"] = text
    return {"message": body}


def cb(uid, data):
    return {"callback_query": {"id": "q", "data": data, "from": msg(uid)["message"]["from"]}}


# ---------- разбор ----------
def test_money():
    assert money.parse("30 000") == 3_000_000
    assert money.parse("15,5") == 1550
    for bad in ("", "-1", "1e9", "nan", "abc", "1.234", True, None, "99999999999"):
        with pytest.raises(ValueError):
            money.parse(bad)
    assert money.convert(1550, 9137) == 141623


def test_links():
    assert parse_link("вот https://t.me/send?start=CQAbc123 держи") == ("cryptobot", "send", "CQAbc123")
    assert parse_link("t.me/CryptoBot?start=CQAbc123")[0] == "cryptobot"
    assert parse_link("https://t.me/xrocket?start=t_abc123")[0] == "xrocket"
    assert parse_link("t.me/tonRocketBot?start=mc_abc123")[0] == "xrocket"
    for bad in ("t.me/CryptoTestnetBot?start=CQAbc123", "t.me/send_fake?start=CQAbc123",
                "t.me/send?start=IVabc123", "evil.com/t.me/send?start=CQAbc123",
                "t.me/xrocket?start=ref_123", "привет"):
        assert parse_link(bad) is None, bad


def test_replies():
    r = parse_reply("💰 Вы получили 🪙 10.5 USDT ($10.50).")
    assert (r.status, r.usd) == ("ok", 1050)
    assert parse_reply("You received 1,000.25 USDT").usd == 100025
    assert parse_reply("Вы получили 5 TON ($12)").status == "review"
    assert parse_reply("Вы получили 1,000 USDT").status == "review"
    assert parse_reply("Этот чек уже активирован.").status == "failed"
    # комментарий отправителя не влияет на сумму
    assert parse_reply("Вы получили 1 USDT\n💬 Вы получили 9999 USDT").usd == 100
    assert parse_reply("Новое сообщение: Вы получили 9999 USDT") is None
    assert parse_reply("Ваш баланс: 500 USDT") is None
    assert parse_reply("🎉 Поздравляем, вы получили приз, введите пароль") is None


# ---------- чеки ----------
def test_deposit_once(env):
    link = "t.me/send?start=CQAAAA1111"
    first = env.dep.submit(CLIENT, link)
    with pytest.raises(Err):
        env.dep.submit(OTHER, link)
    run(env.dep.process(first, "send"))
    run(env.dep.process(first, "send"))
    assert env.balance == 10000
    with pytest.raises(Err):
        env.dep.submit(CLIENT, link)


@pytest.mark.parametrize("boom,status", [("before", "failed"), ("after", "review")])
def test_deposit_errors(env, boom, status):
    env.account.boom = boom
    dep = env.dep.submit(CLIENT, "t.me/send?start=CQAAAA1111")
    run(env.dep.process(dep, "send"))
    assert env.dep.get(dep)["status"] == status and env.balance == 0


def test_big_cheque_goes_to_review(env):
    env.account.result = Result("ok", usd=500_000, asset="USDT", amount="5000")
    dep = env.dep.submit(CLIENT, "t.me/send?start=CQAAAA1111")
    run(env.dep.process(dep, "send"))
    assert env.balance == 0 and env.dep.get(dep)["status"] == "review"
    with pytest.raises(Err):
        run(env.dep.resolve(ADMIN, dep, "credit", "5000"))
    run(env.dep.resolve(OWNER, dep, "credit", "5000"))
    with pytest.raises(Err):
        run(env.dep.resolve(OWNER, dep, "credit", "5000"))
    assert env.balance == 500_000


def test_restart_recovery(env):
    a = env.dep.submit(CLIENT, "t.me/send?start=CQAAAA1111")
    b = env.dep.submit(CLIENT, "t.me/xrocket?start=t_BBBB2222")
    env.dep._update(a, status="processing")
    env.dep.queue = asyncio.Queue()
    env.dep.recover()
    assert env.dep.get(a)["status"] == "review"
    assert env.dep.queue.get_nowait() == (b, "xrocket")


# ---------- роли и настройки ----------
def test_roles(env):
    assert env.core.role(OWNER) == "owner" and env.core.role(ADMIN) == "admin"
    env.core.touch_user(OWNER, "renamed", "o")
    env.core.touch_user(OTHER, "ownerparadoxy", "thief")
    assert env.core.role(OWNER) == "owner" and env.core.role(OTHER) is None
    with pytest.raises(Err):
        env.core.add_admin(ADMIN, "someone")
    with pytest.raises(Err):
        run(env.core.remove_admin(OWNER, "ownerparadoxy"))


def test_rate_protection(env):
    with pytest.raises(Err, match="RATE_CONFIRM"):
        env.core.update_settings(ADMIN, {"rate": "900"})
    env.core.update_settings(ADMIN, {"rate": "900", "confirm_rate": True})
    for bad in ({"rate": "0"}, {"min_usd": "0"}, {"hours_from": "25:00"}, {"timer_min": 0}, {"tz_offset": "nan"}):
        with pytest.raises(Err):
            env.core.update_settings(OWNER, bad)


def test_work_hours(env):
    from datetime import datetime, timezone
    env.t = int(datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc).timestamp())  # пт 23:00 МСК
    env.core.update_settings(OWNER, {"hours_enabled": True, "hours_from": "10:00", "hours_to": "22:00"})
    assert not env.core.work_state()[0]
    env.core.update_settings(OWNER, {"hours_from": "22:00", "hours_to": "06:00", "work_days": "5"})
    assert env.core.work_state()[0]
    env.t += 5 * 3600  # сб 04:00 — хвост пятничной смены
    assert env.core.work_state()[0]
    env.t += 20 * 3600  # вс 00:00 — смена субботы выключена
    assert not env.core.work_state()[0]


# ---------- заявки ----------
def test_order_validation(env):
    env.fund(20000)
    for kwargs in (dict(usd="14.99"), dict(usd="201"), dict(phone="123"), dict(fio="Иван"), dict(bank=""),
                   dict(rate_seen="8000"), dict(usd="-20")):
        with pytest.raises(Err):
            run(env.order(**{"usd": "20", **kwargs}))
    assert env.balance == 20000
    one = run(env.order("15", request_key="same-key-1"))
    two = run(env.order("15", request_key="same-key-1"))
    assert one == two and env.balance == 18500


def test_full_flow(env):
    env.fund()

    async def scenario():
        oid = await env.order()
        await env.bot.handle(cb(ADMIN, f"acc:{oid}"))
        with pytest.raises(Err):
            await env.core.accept(OWNER, oid)
        await env.bot.handle(cb(ADMIN, f"give:{oid}"))
        await env.bot.handle(msg(ADMIN, "900000", 5))
        assert "слишком большая сумма" in env.api.texts(ADMIN)[-1]
        await env.bot.handle(msg(ADMIN, "9 000", 6))
        assert not env.core.payments(oid)  # без подтверждения выплаты нет
        await env.bot.handle(cb(ADMIN, "ok:6"))
        await env.bot.handle(cb(ADMIN, "ok:6"))
        pay = env.core.payments(oid)
        assert len(pay) == 1 and pay[0]["amount"] == 900000
        assert pay[0]["deadline"] - env.t == 900
        buttons = [p for m, p in env.api.calls if m == "sendMessage" and p["chat_id"] == CLIENT
                   and "отправлено" in p["text"]][0]["reply_markup"]["inline_keyboard"][0]
        assert [b["style"] for b in buttons] == ["success", "danger"]
        pid = pay[0]["id"]
        await env.bot.handle(cb(OTHER, f"got:{pid}"))
        assert env.core.payment(pid)["status"] == "sent"
        await env.bot.handle(cb(CLIENT, f"nogot:{pid}"))
        code = env.core.payment(pid)["video_code"]
        assert len(code) == 4 and code.isdigit()
        await env.bot.handle(cb(CLIENT, f"nogot:{pid}"))
        assert env.core.payment(pid)["video_code"] == code
        await env.bot.handle(msg(CLIENT, mid=9, video={"file_id": "v"}))
        assert env.core.payment(pid)["video_received"] == 1
        assert ("copyMessage", {"chat_id": ADMIN, "from_chat_id": CLIENT, "message_id": 9}) in env.api.calls
        with pytest.raises(Err):
            await env.core.complete(ADMIN, oid)
        with pytest.raises(Err):
            await env.core.void_payment(ADMIN, pid)
        with pytest.raises(Err):
            await env.core.cancel(ADMIN, oid)
        # переписка в обе стороны
        await env.bot.handle(cb(ADMIN, f"chat:{oid}"))
        await env.bot.handle(msg(ADMIN, "Проверьте <ещё> раз", 10))
        assert "Проверьте &lt;ещё&gt; раз" in env.api.texts(CLIENT)[-1]
        await env.bot.handle(cb(CLIENT, f"chat:{oid}"))
        await env.bot.handle(msg(CLIENT, "Сейчас", 11))
        assert "Сейчас" in env.api.texts(ADMIN)[-1]
        await env.bot.handle(cb(CLIENT, f"got:{pid}"))
        await env.bot.handle(cb(ADMIN, f"done:{oid}"))
        assert env.core.order(oid)["status"] == "active"  # сначала подтверждение
        await env.bot.handle(cb(ADMIN, f"doneok:{oid}:0"))
        assert env.core.order(oid)["status"] == "done" and env.balance == 0
        with pytest.raises(Err):
            await env.core.cancel(ADMIN, oid)

    run(scenario())


def test_timer_and_partial_cancel(env):
    env.fund()

    async def scenario():
        oid = await env.order()
        await env.core.accept(ADMIN, oid)
        first = await env.core.give(ADMIN, oid, "3000")
        with pytest.raises(Err):
            await env.core.give(ADMIN, oid, "100")
        await env.core.received(CLIENT, first)
        with pytest.raises(Err, match="слишком большая"):
            await env.core.give(ADMIN, oid, "6000.01")
        second = await env.core.give(ADMIN, oid, "6000")
        with pytest.raises(Err, match="Таймер"):
            await env.core.complete(ADMIN, oid)
        env.t += 901
        await env.core.expire_timers()
        count = len(env.api.calls)
        await env.core.expire_timers()
        assert len(env.api.calls) == count
        await env.core.void_payment(ADMIN, second)
        await env.core.cancel(ADMIN, oid)
        assert env.balance == 6666  # треть выплачена — две трети вернулись
        with pytest.raises(Err):
            await env.core.cancel(ADMIN, oid)

    run(scenario())


@pytest.mark.parametrize("paid,refund", [(None, 10000), ("3000", 6666), ("9000", 0)])
def test_complete_returns_rest(env, paid, refund):
    env.fund()

    async def scenario():
        oid = await env.order()
        await env.core.accept(ADMIN, oid)
        if paid:
            await env.core.received(CLIENT, await env.core.give(ADMIN, oid, paid))
        await env.bot.handle(cb(ADMIN, f"done:{oid}"))
        await env.bot.handle(cb(ADMIN, f"doneok:{oid}:{refund}"))
        await env.bot.handle(cb(ADMIN, f"doneok:{oid}:{refund}"))
        order = env.core.order(oid)
        assert (order["status"], order["refunded"], env.balance) == ("done", refund, refund)

    run(scenario())


def test_complete_guards(env):
    env.fund()

    async def scenario():
        oid = await env.order()
        await env.core.accept(ADMIN, oid)
        pid = await env.core.give(ADMIN, oid, "3000")
        with pytest.raises(Err, match="Таймер"):
            await env.core.complete(ADMIN, oid)
        env.t += 901
        with pytest.raises(Err, match="изменились"):
            await env.core.complete(ADMIN, oid, expect=10000)
        await env.core.not_received(CLIENT, pid)
        with pytest.raises(Err, match="спор"):
            await env.core.complete(ADMIN, oid)
        with pytest.raises(Err):
            await env.core.complete(ADMIN, oid, force=True)
        assert env.balance == 0
        await env.core.complete(OWNER, oid, force=True)
        assert env.balance == 6666

    run(scenario())


def test_silent_client_counts_as_paid(env):
    env.fund()

    async def scenario():
        oid = await env.order()
        await env.core.accept(ADMIN, oid)
        await env.core.give(ADMIN, oid, "4500")
        env.t += 901
        await env.core.complete(ADMIN, oid)
        assert env.balance == 5000

    run(scenario())


def test_user_cancel_and_removed_admin(env):
    env.fund()

    async def scenario():
        a = await env.order("20")
        await env.core.cancel_by_user(CLIENT, a)
        assert env.balance == 10000
        b = await env.order("20")
        await env.core.accept(ADMIN, b)
        with pytest.raises(Err):
            await env.core.cancel_by_user(CLIENT, b)
        await env.core.remove_admin(OWNER, "manager")
        assert env.core.order(b)["status"] == "new"
        with pytest.raises(Err):
            await env.core.accept(ADMIN, b)

    run(scenario())


def test_bot_cheque_message(env):
    run(env.bot.handle(msg(CLIENT, "https://t.me/send?start=CQZZZZ9999")))
    assert env.dep.queue.qsize() == 1


def test_client_cheque_never_reaches_manager(env):
    env.fund()
    link = "t.me/send?start=CQCHAT12345"

    async def scenario():
        oid = await env.order()
        await env.core.accept(ADMIN, oid)
        await env.bot.handle(cb(CLIENT, f"chat:{oid}"))
        await env.bot.handle(msg(CLIENT, link, 20))
        assert env.dep.queue.qsize() == 1 and not env.core.messages(oid)
        with pytest.raises(Err):
            await env.core.add_message(CLIENT, oid, link)
        assert not any(link in t for t in env.api.texts(ADMIN))
        with pytest.raises(Err):
            env.core.update_settings(ADMIN, {"auto_credit_max": "999999"})

    run(scenario())


# ---------- HTTP ----------
def init_data(uid, username, token=TOKEN, age=0):
    data = {"auth_date": str(int(time.time()) - age),
            "user": json.dumps({"id": uid, "username": username, "first_name": "<img src=x>"})}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Init-Data": urlencode(data)}


def test_web(env):
    client = TestClient(create_app(env.core, env.dep, env.account))
    assert client.get("/health").text == "ok"
    assert "TwaragaSwap" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../main.py").status_code == 404
    for headers in ({}, init_data(CLIENT, "client", "999:BAD"), init_data(CLIENT, "client", age=90000),
                    {"X-Init-Data": "hash=x"}):
        assert client.get("/api/state", headers=headers).status_code == 401
    me = init_data(CLIENT, "client")
    state = client.get("/api/state", headers=me).json()
    assert "admin" not in state
    for name in ("admin.settings", "admin.add", "admin.balance", "account.logout", "order.accept"):
        r = client.post(f"/api/{name}", headers=me, json={"id": 1, "values": {"rate": "1"}, "username": "x"})
        assert r.status_code == 400, name
    assert client.post("/api/order.pay", headers=me, content=b"[1]").status_code == 400
    assert client.post("/api/order.pay", headers=me, content=b"x" * 20000).status_code == 413
    env.fund()
    r = client.post("/api/order.create", headers=me, json={
        "usd": "20", "phone": "+79000000000", "bank": "Банк", "fio": "Иванов Иван",
        "rate": "9000", "key": "abcdefgh1"})
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    stranger = init_data(OTHER, "other")
    assert client.get("/api/state", headers=stranger).json()["orders"] == []
    assert client.post("/api/order.message", headers=stranger, json={"id": oid, "text": "x"}).status_code == 400
    admin = client.get("/api/state", headers=init_data(ADMIN, "manager")).json()["admin"]
    assert admin["orders"][0]["id"] == oid and "admins" not in admin
    owner = client.get("/api/state", headers=init_data(OWNER, "OwnerParadoxy")).json()["admin"]
    assert owner["admins"][0]["username"] == "ownerparadoxy"


def test_config_url():
    assert webapp_url("") == "https://gxbtbzht.bothost.tech"
    assert webapp_url("http://a.b/") == "https://a.b"


# ---------- аккаунт ----------
class Msg:
    def __init__(self, id, text, out=False):
        self.id, self.raw_text, self.out = id, text, out


class Entity:
    bot = True


class Client:
    def __init__(self, replies):
        self.replies = replies

    def is_connected(self):
        return True

    async def get_entity(self, name):
        return Entity()

    async def send_message(self, entity, text):
        return Msg(10, text, True)

    async def get_messages(self, entity, min_id, limit):
        return [Msg(i, t, o) for i, t, o in self.replies if i > min_id]


@pytest.mark.parametrize("replies,status,usd", [
    ([(12, "Вы получили 7 USDT", False), (11, "⏳ Обработка…", False)], "ok", 700),
    ([(9, "Вы получили 500 USDT", False)], "review", 0),   # старое сообщение игнорируется
    ([(11, "Вы получили 500 USDT", True)], "review", 0),   # наше исходящее игнорируется
])
def test_activation(tmp_path, replies, status, usd):
    account = Account(Config(TOKEN, "", 1, tmp_path, "o", 0, 0, "", ""))
    account.client, account.me = Client(replies), "@me"
    account.REPLY_TIMEOUT = 2
    result = run(account.activate("send", "CQX", lambda: None))
    assert (result.status, result.usd) == (status, usd)

"""HTTP: мини-приложение и его API."""
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import money
from .account import AccountError
from .core import Err

log = logging.getLogger("web")
STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_BODY = 16_384
AUTH_TTL = 24 * 3600
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; "
                               "style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                               "form-action 'none'; object-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def check_init_data(init_data: str, token: str, now: float):
    """Проверка подписи Telegram WebApp. Возвращает словарь пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received = data.pop("hash")
        check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), received.encode()):
            return None
        age = now - int(data["auth_date"])
        if not -300 <= age <= AUTH_TTL:
            return None
        user = json.loads(data["user"])
        return user if isinstance(user.get("id"), int) else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def create_app(core, deposits, account, lifespan=None):
    db = core.db

    def state(uid):
        s = core.settings()
        ok, why = core.work_state()
        role = core.role(uid)
        data = {
            "user": {"id": uid, "balance": core.user(uid)["balance"], "role": role},
            "now": core.now(),
            "settings": {k: s[k] for k in ("rate", "currency", "min_usd", "max_usd", "timer_min")},
            "open": ok, "closed_reason": why, "deposits_ready": account.ready,
            "deposits": db.all("SELECT id,provider,status,usd,note,created FROM deposits "
                               "WHERE user_id=? ORDER BY updated DESC LIMIT 15", uid),
            "orders": [core.order_view(uid, r["id"]) for r in db.all(
                "SELECT id FROM orders WHERE user_id=? ORDER BY status IN ('new','active') DESC, id DESC "
                "LIMIT 15", uid)],
        }
        if role:
            mine = "" if role == "owner" else " AND (manager_id IS NULL OR manager_id=%d)" % uid
            admin = {
                "settings": s,
                "orders": [core.order_view(uid, r["id"]) for r in db.all(
                    "SELECT id FROM orders WHERE 1=1" + mine +
                    " ORDER BY status IN ('new','active') DESC, id DESC LIMIT 40")],
            }
            if role == "owner":
                admin.update(
                    admins=core.admins(), account=account.status(),
                    review=db.all("SELECT d.*, u.username, u.name FROM deposits d JOIN users u ON u.id=d.user_id "
                                  "WHERE d.status='review' ORDER BY d.id"),
                    totals=db.one("SELECT COUNT(*) users, COALESCE(SUM(balance),0) balance FROM users"))
            data["admin"] = admin
        return data

    async def act(uid, name, body):
        num = lambda key="id": int(body[key])
        if name == "deposit":
            deposits.submit(uid, str(body.get("link", ""))[:300])
        elif name == "order.create":
            return {"order_id": await core.create_order(
                uid, body.get("usd"), body.get("phone"), body.get("bank"), body.get("fio"),
                body.get("rate"), body.get("key"))}
        elif name == "order.cancel_own":
            await core.cancel_by_user(uid, num())
        elif name == "order.accept":
            await core.accept(uid, num())
        elif name == "order.release":
            await core.release(uid, num())
        elif name == "order.pay":
            await core.give(uid, num(), body.get("amount"))
        elif name == "order.check_pay":
            return {"amount": core.check_amount(uid, num(), body.get("amount"))}
        elif name == "order.complete":
            await core.complete(uid, num(), bool(body.get("force")), body.get("expect"))
        elif name == "order.cancel":
            await core.cancel(uid, num(), bool(body.get("force")))
        elif name == "order.message":
            await core.add_message(uid, num(), body.get("text"))
        elif name == "payment.received":
            await core.received(uid, num())
        elif name == "payment.not_received":
            await core.not_received(uid, num())
        elif name == "payment.void":
            await core.void_payment(uid, num())
        elif name == "admin.settings":
            core.update_settings(uid, dict(body.get("values") or {}))
        elif name == "admin.add":
            core.add_admin(uid, body.get("username"))
        elif name == "admin.remove":
            await core.remove_admin(uid, body.get("username"))
        elif name == "admin.balance":
            await core.adjust_balance(uid, body.get("user"), body.get("delta"), body.get("reason"))
        elif name == "admin.deposit":
            await deposits.resolve(uid, num(), body.get("decision"), body.get("usd"))
        elif name.startswith("account."):
            core.require_owner(uid)
            if name == "account.send_code":
                await account.send_code(body.get("api_id"), body.get("api_hash"), body.get("phone"))
            elif name == "account.sign_in":
                await account.sign_in(body.get("code", ""), body.get("password", ""))
            elif name == "account.logout":
                await account.logout()
            else:
                raise Err("Неизвестное действие")
        else:
            raise Err("Неизвестное действие")
        return {}

    def authorize(request: Request):
        user = check_init_data(request.headers.get("x-init-data", ""), core.config.bot_token, time.time())
        if not user:
            return None
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "—"
        core.touch_user(user["id"], user.get("username"), name)
        return user["id"]

    def reply(data, status=200):
        return JSONResponse(data, status, headers={"Cache-Control": "no-store", **HEADERS})

    async def api_state(request):
        uid = authorize(request)
        if uid is None:
            return reply({"error": "Откройте приложение из Telegram заново"}, 401)
        return reply(state(uid))

    async def api_action(request):
        uid = authorize(request)
        if uid is None:
            return reply({"error": "Откройте приложение из Telegram заново"}, 401)
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                return reply({"error": "Слишком большой запрос"}, 413)
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            result = await act(uid, request.path_params["name"], body)
        except (Err, AccountError) as exc:
            return reply({"error": str(exc)}, 400)
        except (ValueError, KeyError, TypeError, OverflowError):
            return reply({"error": "Некорректный запрос"}, 400)
        except Exception:
            log.exception("action")
            return reply({"error": "Внутренняя ошибка, попробуйте ещё раз"}, 500)
        return reply({"ok": True, **result, "state": state(uid)})

    async def index(request):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache", **HEADERS})

    async def health(request):
        return PlainTextResponse("ok")

    return Starlette(lifespan=lifespan, routes=[
        Route("/", index), Route("/health", health),
        Route("/api/state", api_state, methods=["GET"]),
        Route("/api/{name}", api_action, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ])

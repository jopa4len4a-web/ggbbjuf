"""Разбор ссылок на чеки CryptoBot / xRocket и ответов этих ботов."""
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

# Только официальные боты. Ссылки на любые другие боты (в т.ч. тестнет) не принимаются.
BOTS = {
    "cryptobot": ("cryptobot", "send"),
    "xrocket": ("xrocket", "tonrocketbot"),
}
CODE_PREFIX = {"cryptobot": r"CQ", "xrocket": r"(?:t_|mc_|mci_)"}
STABLE = {"USDT", "USDC"}

LINK_RE = re.compile(
    r"(?<![\w./-])(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?P<bot>[A-Za-z0-9_]+)(?:/app)?\?start(?:app)?=(?P<code>[A-Za-z0-9_-]{4,64})(?![\w-])", re.I)


def parse_link(text: str):
    """(provider, bot_username, code) или None."""
    for match in LINK_RE.finditer(text or ""):
        bot, code = match["bot"].lower(), match["code"]
        for provider, names in BOTS.items():
            if bot in names and re.match(CODE_PREFIX[provider], code):
                return provider, bot, code
    return None


@dataclass
class Result:
    status: str              # ok | failed | review
    usd: int = 0             # центы
    asset: str = ""
    amount: str = ""
    note: str = ""
    reply: str = ""


# Шаблон привязан к НАЧАЛУ сообщения: в чеке бывает комментарий отправителя,
# и он не должен влиять на зачисляемую сумму.
SUCCESS_RE = re.compile(
    r"^[^\w$]{0,12}"
    r"(?:(?:Чек активирован|Cheque activated|Check activated)[^\w$]{1,12})?"
    r"(?:Вы получили|Вы получаете|You received|You have received|You got)"
    r"[^\w$]{1,12}"
    r"(?P<num>\d[\d  ,]*(?:\.\d+)?)[  ]*"
    r"(?P<asset>[A-Z][A-Z0-9]{1,11})(?![\w])", re.I)

FAILURES = (
    (r"already (been )?(activated|claimed|used)|уже (был )?(активирован|использован|получен)", "Чек уже активирован"),
    (r"not found|не найден|не существует", "Чек не найден"),
    (r"expired|истек|истёк", "Срок действия чека истёк"),
    (r"password|парол", "Чек с паролем. Создайте чек без пароля"),
    (r"intended for|for another user|only for|предназначен (для )?друго|не для вас", "Чек выписан на другого пользователя"),
    (r"subscribe|подпис", "Чек требует подписку на канал. Создайте чек без условий"),
    (r"premium", "Чек только для Premium-пользователей. Создайте обычный чек"),
    (r"cannot activate your own|свой (собственный )?чек", "Нельзя активировать этот чек"),
)


def parse_number(raw: str):
    raw = raw.replace(" ", "").replace(" ", "")
    if "," in raw:
        # Запятая допустима только как разделитель тысяч при наличии точки: 1,000.50
        if not re.fullmatch(r"\d{1,3}(,\d{3})+\.\d+", raw):
            return None
        raw = raw.replace(",", "")
    return Decimal(raw)


def parse_reply(text: str):
    """Result, если сообщение понятно, иначе None."""
    text = (text or "").strip()
    match = SUCCESS_RE.match(text)
    if match:
        number = parse_number(match["num"])
        asset = match["asset"].upper()
        base = dict(asset=asset, amount=match["num"].strip(), reply=text[:600])
        if number is None or number <= 0:
            return Result("review", note="Не удалось однозначно прочитать сумму", **base)
        if asset not in STABLE:
            return Result("review", note=f"Валюта {asset}: нужна ручная оценка", **base)
        cents = int((number * 100).to_integral_value(ROUND_DOWN))
        return Result("ok", usd=cents, **base)
    head = text[:300].lower()
    # Похоже на получение денег, но формат незнакомый — не объявляем отказ, пусть проверит человек.
    if re.search(r"вы получ|you (have )?(received|got)", head):
        return None
    for pattern, reason in FAILURES:
        if re.search(pattern, head):
            return Result("failed", note=reason, reply=text[:600])
    return None

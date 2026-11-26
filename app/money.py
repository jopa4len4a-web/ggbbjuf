"""Деньги хранятся целыми числами: центы USD и копейки валюты выплаты."""
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN

MAX_UNITS = Decimal("1000000000")


def parse(value) -> int:
    """'30 000', '15,5', 15.5 -> минимальные единицы. ValueError при мусоре."""
    if isinstance(value, bool) or value is None:
        raise ValueError("empty")
    text = re.sub(r"[\s_]", "", str(value)).replace(",", ".")
    if not re.fullmatch(r"\d{1,10}(\.\d{1,2})?", text):
        raise ValueError("format")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("format") from exc
    if number > MAX_UNITS:
        raise ValueError("too big")
    return int(number * 100)


def fmt(units: int) -> str:
    sign = "-" if units < 0 else ""
    whole, frac = divmod(abs(int(units)), 100)
    body = f"{whole:,}".replace(",", " ")
    return f"{sign}{body}" + (f",{frac:02d}" if frac else "")


def convert(usd_cents: int, rate_units: int) -> int:
    return int((Decimal(usd_cents) * Decimal(rate_units) / 100).to_integral_value(ROUND_DOWN))

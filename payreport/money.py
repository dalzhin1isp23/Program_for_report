"""Деньги — целые копейки. Float не используется нигде."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_SPACES = (" ", "\u00a0", "\u202f", "\u2009", "\t")


class MoneyError(ValueError):
    pass


def parse_kop(raw: str | None) -> int:
    """'1 234,56' -> 123456. Больше двух знаков — ошибка, а не тихое округление."""
    if raw is None:
        raise MoneyError("пустая сумма")
    s = raw.strip()
    for ch in _SPACES:
        s = s.replace(ch, "")
    s = s.replace(",", ".")
    if not s:
        raise MoneyError("пустая сумма")
    if not _NUMBER.match(s):
        raise MoneyError(f"не похоже на сумму: {raw!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise MoneyError(f"не похоже на сумму: {raw!r}") from exc
    kop = value * 100
    if kop != kop.to_integral_value():
        raise MoneyError(f"больше двух знаков после запятой: {raw!r}")
    return int(kop)


def fmt(kop: int, *, group: str = " ", dec: str = ",") -> str:
    sign = "-" if kop < 0 else ""
    rub, k = divmod(abs(kop), 100)
    digits = str(rub)
    if group:
        chunks = []
        while len(digits) > 3:
            chunks.append(digits[-3:])
            digits = digits[:-3]
        chunks.append(digits)
        digits = group.join(reversed(chunks))
    return f"{sign}{digits}{dec}{k:02d}"


def fmt_csv(kop: int) -> str:
    return fmt(kop, group="", dec=",")   # для русского Excel


def fmt_json(kop: int) -> str:
    return fmt(kop, group="", dec=".")   # точная строка, не float
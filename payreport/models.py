from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount_kop: int
    signed_at: date | None
    row: int


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price_kop: int
    status: str                    # как в выгрузке
    completed_at: date | None
    cancelled_at: date | None
    row: int


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None            # None = платёж по договору без привязки
    purpose: str
    amount_kop: int
    status: str
    created_at: date | None
    paid_at: date | None
    refunded_at: date | None
    row: int


@dataclass(frozen=True)
class Issue:
    severity: str                  # error | warning | info
    source: str
    code: str
    message: str
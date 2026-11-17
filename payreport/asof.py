"""Состояние НА ДАТУ. Статусы в выгрузке — на момент выгрузки, не на дату отчёта."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Payment, Work

DONE, PLANNED, CANCELLED = "done", "planned", "cancelled"


def work_state(work: Work, on: date) -> str:
    if work.cancelled_at is not None:
        return CANCELLED if work.cancelled_at <= on else PLANNED
    if work.completed_at is not None:
        return DONE if work.completed_at <= on else PLANNED
    # дат события нет (уже отмечено warning при загрузке) — верим статусу выгрузки
    return work.status if work.status in (DONE, CANCELLED) else PLANNED


@dataclass(frozen=True)
class Effect:
    gross_kop: int      # ушло подрядчику на дату
    refunded_kop: int   # вернулось на дату
    net_kop: int        # реально заплачено на дату
    queued_kop: int     # в очереди, деньги не ушли
    state: str          # not_paid | queued | paid | refunded


def payment_effect(p: Payment, on: date) -> Effect:
    left = p.paid_at is not None and p.paid_at <= on
    if not left:
        existed = p.created_at is None or p.created_at <= on
        queued = p.amount_kop if p.status == "queued" and existed else 0
        return Effect(0, 0, 0, queued, "queued" if queued else "not_paid")
    if p.refunded_at is not None and p.refunded_at <= on:
        return Effect(p.amount_kop, p.amount_kop, 0, 0, "refunded")
    return Effect(p.amount_kop, 0, p.amount_kop, 0, "paid")
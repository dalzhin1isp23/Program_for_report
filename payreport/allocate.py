"""Разнесение платежей по работам.

1. Привязанные платежи — на свою работу, не больше её цены; излишек виден отдельно.
2. Остаток (платежи без привязки) — по неотменённым работам:
   fifo         — водопад: сначала полностью гасим принятые работы по дате приёмки, затем плановые;
   proportional — пропорционально непокрытому остатку цены, метод наибольшего остатка.
Каждая копейка привязана к конкретному payment_id — это то, что бухгалтер сейчас делает руками.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .asof import CANCELLED, DONE, payment_effect, work_state
from .models import Payment, Work

STRATEGIES = ("fifo", "proportional")


@dataclass(frozen=True)
class Piece:
    payment_id: str
    work_id: str
    kind: str            # linked | pool
    amount_kop: int


@dataclass
class WorkAlloc:
    work_id: str
    state: str
    price_kop: int
    paid_kop: int = 0
    linked_kop: int = 0
    pool_kop: int = 0

    @property
    def remaining_kop(self) -> int:
        return 0 if self.state == CANCELLED else self.price_kop - self.paid_kop


@dataclass
class ContractAlloc:
    works: dict[str, WorkAlloc]
    pieces: list[Piece] = field(default_factory=list)
    unallocated: list[tuple[str, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def order_key(w: Work, state: str):
    return (0 if state == DONE else 1, w.completed_at or date.max, w.work_id)


def _largest_remainder(amount: int, ids: list[str], needs: dict[str, int]) -> dict[str, int]:
    ids = [i for i in ids if needs[i] > 0]
    total = sum(needs[i] for i in ids)
    if not ids or total == 0 or amount == 0:
        return {}
    amount = min(amount, total)
    shares = {i: amount * needs[i] // total for i in ids}
    rest = amount - sum(shares.values())
    by_rem = sorted(ids, key=lambda i: (-(amount * needs[i] % total), i))
    for i in by_rem[:rest]:
        shares[i] += 1
    return shares


def allocate_contract(works: list[Work], payments: list[Payment],
                      on: date, strategy: str = "fifo") -> ContractAlloc:
    if strategy not in STRATEGIES:
        raise ValueError(f"неизвестная стратегия {strategy!r}")

    states = {w.work_id: work_state(w, on) for w in works}
    acc = {w.work_id: WorkAlloc(w.work_id, states[w.work_id], w.price_kop) for w in works}
    res = ContractAlloc(works=acc)
    chrono = sorted(payments, key=lambda p: (p.paid_at or date.max, p.payment_id))
    pool: list[tuple[str, int]] = []

    # 1) привязанные платежи
    for p in chrono:
        eff = payment_effect(p, on)
        if eff.net_kop <= 0:
            continue
        target = acc.get(p.work_id) if p.work_id else None
        if target is None:
            pool.append((p.payment_id, eff.net_kop))
            continue
        if target.state == CANCELLED:
            take = eff.net_kop
            res.notes.append(f"{p.payment_id}: {target.work_id} отменена на дату отчёта — "
                             f"деньги показаны на работе, нужен возврат или зачёт")
        else:
            take = min(target.price_kop - target.paid_kop, eff.net_kop)
        if take:
            target.paid_kop += take
            target.linked_kop += take
            res.pieces.append(Piece(p.payment_id, target.work_id, "linked", take))
        rest = eff.net_kop - take
        if rest:
            res.unallocated.append((p.payment_id, rest))
            res.notes.append(f"{p.payment_id}: превышает цену {target.work_id} — "
                             f"переплата не разнесена по другим работам")

    # 2) общий пул договора
    active = sorted([w for w in works if states[w.work_id] != CANCELLED],
                    key=lambda w: order_key(w, states[w.work_id]))
    needs = {w.work_id: max(0, w.price_kop - acc[w.work_id].paid_kop) for w in active}
    pool_total = sum(a for _, a in pool)

    if strategy == "fifo":
        targets, left = {}, pool_total
        for w in active:
            take = min(needs[w.work_id], left)
            targets[w.work_id] = take
            left -= take
    else:
        targets = _largest_remainder(pool_total, [w.work_id for w in active], needs)

    queue = list(pool)
    qi, qleft = 0, (queue[0][1] if queue else 0)
    for w in active:
        want = targets.get(w.work_id, 0)
        while want > 0 and qi < len(queue):
            take = min(want, qleft)
            pid = queue[qi][0]
            acc[w.work_id].paid_kop += take
            acc[w.work_id].pool_kop += take
            res.pieces.append(Piece(pid, w.work_id, "pool", take))
            want -= take
            qleft -= take
            if qleft == 0:
                qi += 1
                qleft = queue[qi][1] if qi < len(queue) else 0
    if qi < len(queue):
        if qleft:
            res.unallocated.append((queue[qi][0], qleft))
        res.unallocated.extend(queue[qi + 1:])

    return res
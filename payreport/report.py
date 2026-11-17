from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .allocate import STRATEGIES, ContractAlloc, Piece, allocate_contract
from .asof import CANCELLED, payment_effect, work_state
from .loading import LoadResult
from .models import Issue
from .money import fmt


class InvariantError(AssertionError):
    pass


@dataclass
class WorkRow:
    contract_id: str
    work_id: str
    title: str
    state: str
    status_export: str
    price_kop: int
    paid_kop: int
    remaining_kop: int
    linked_kop: int
    pool_kop: int
    queued_kop: int
    refunded_kop: int
    pieces: list[Piece] = field(default_factory=list)


@dataclass
class ContractRow:
    contract_id: str
    contractor: str
    amount_kop: int
    works: list[WorkRow]
    works_price_kop: int
    active_price_kop: int
    cancelled_price_kop: int
    gross_paid_kop: int
    refunded_kop: int
    paid_kop: int
    queued_kop: int
    allocated_kop: int
    unallocated_kop: int
    paid_on_cancelled_kop: int
    remaining_by_amount_kop: int
    remaining_by_works_kop: int
    overpaid_kop: int
    delta_kop: int
    pieces: list[Piece]
    unallocated_detail: list[tuple[str, int]]


@dataclass
class Report:
    as_of: date
    strategy: str
    contracts: list[ContractRow]
    issues: list[Issue]

    @property
    def works(self) -> list[WorkRow]:
        return [w for c in self.contracts for w in c.works]


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise InvariantError(msg)


def build_report(data: LoadResult, on: date, strategy: str = "fifo") -> Report:
    if strategy not in STRATEGIES:
        raise ValueError(strategy)
    issues = list(data.issues)
    works_by_c, pays_by_c = defaultdict(list), defaultdict(list)
    for w in data.works:
        works_by_c[w.contract_id].append(w)
    for p in data.payments:
        pays_by_c[p.contract_id].append(p)

    rows: list[ContractRow] = []
    for c in data.contracts:
        ws = sorted(works_by_c[c.contract_id], key=lambda w: w.work_id)
        ps = pays_by_c[c.contract_id]
        alloc: ContractAlloc = allocate_contract(ws, ps, on, strategy)
        effects = {p.payment_id: payment_effect(p, on) for p in ps}

        work_rows: list[WorkRow] = []
        for w in ws:
            a = alloc.works[w.work_id]
            linked = [p for p in ps if p.work_id == w.work_id]
            work_rows.append(WorkRow(
                contract_id=c.contract_id, work_id=w.work_id, title=w.title,
                state=a.state, status_export=w.status, price_kop=w.price_kop,
                paid_kop=a.paid_kop, remaining_kop=a.remaining_kop,
                linked_kop=a.linked_kop, pool_kop=a.pool_kop,
                queued_kop=sum(effects[p.payment_id].queued_kop for p in linked),
                refunded_kop=sum(effects[p.payment_id].refunded_kop for p in linked),
                pieces=[x for x in alloc.pieces if x.work_id == w.work_id],
            ))
            if a.state != w.status:
                issues.append(Issue("info", f"works.csv:{w.row}", "status_asof",
                                    f"{w.work_id}: в выгрузке {w.status}, на {on:%d.%m.%Y} — {a.state}"))

        for p in ps:
            eff = effects[p.payment_id]
            if p.paid_at and p.paid_at > on:
                issues.append(Issue("info", f"payments.csv:{p.row}", "after_date",
                                    f"{p.payment_id}: оплата {p.paid_at:%d.%m.%Y} позже даты отчёта — "
                                    f"{fmt(p.amount_kop)} не учтены"))
            if eff.state == "refunded":
                issues.append(Issue("info", f"payments.csv:{p.row}", "refunded",
                                    f"{p.payment_id}: возвращён {p.refunded_at:%d.%m.%Y} — "
                                    f"{fmt(p.amount_kop)} не входят в «выплачено»"))

        gross = sum(e.gross_kop for e in effects.values())
        refunded = sum(e.refunded_kop for e in effects.values())
        net = sum(e.net_kop for e in effects.values())
        queued = sum(e.queued_kop for e in effects.values())
        allocated = sum(a.paid_kop for a in alloc.works.values())
        unallocated = sum(a for _, a in alloc.unallocated)
        on_cancelled = sum(a.paid_kop for a in alloc.works.values() if a.state == CANCELLED)
        works_price = sum(w.price_kop for w in ws)
        active_price = sum(w.price_kop for w in ws if work_state(w, on) != CANCELLED)
        rem_amount = c.amount_kop - net
        rem_works = active_price - (allocated - on_cancelled)

        # Инварианты: лучше упасть, чем показать неверные деньги.
        _check(allocated + unallocated == net,
               f"{c.contract_id}: разнесено {allocated} + не разнесено {unallocated} != выплачено {net}")
        per_payment: dict[str, int] = defaultdict(int)
        for x in alloc.pieces:
            per_payment[x.payment_id] += x.amount_kop
        for pid, amount in alloc.unallocated:
            per_payment[pid] += amount
        for p in ps:
            _check(per_payment[p.payment_id] == effects[p.payment_id].net_kop,
                   f"{p.payment_id}: суммы по работам не сходятся с суммой платежа")
        for a in alloc.works.values():
            _check(a.state == CANCELLED or 0 <= a.paid_kop <= a.price_kop,
                   f"{a.work_id}: выплачено вне диапазона 0..цена")

        row = ContractRow(
            contract_id=c.contract_id, contractor=c.contractor, amount_kop=c.amount_kop,
            works=work_rows, works_price_kop=works_price, active_price_kop=active_price,
            cancelled_price_kop=works_price - active_price,
            gross_paid_kop=gross, refunded_kop=refunded, paid_kop=net, queued_kop=queued,
            allocated_kop=allocated, unallocated_kop=unallocated,
            paid_on_cancelled_kop=on_cancelled,
            remaining_by_amount_kop=rem_amount, remaining_by_works_kop=rem_works,
            overpaid_kop=max(0, net - c.amount_kop), delta_kop=rem_amount - rem_works,
            pieces=alloc.pieces, unallocated_detail=list(alloc.unallocated),
        )
        rows.append(row)

        if works_price != c.amount_kop:
            issues.append(Issue("warning", f"contracts.csv:{c.row}", "sum_mismatch",
                                f"{c.contract_id}: сумма цен работ {fmt(works_price)} != "
                                f"суммы договора {fmt(c.amount_kop)}"))
        if row.cancelled_price_kop and row.delta_kop:
            issues.append(Issue("warning", f"contracts.csv:{c.row}", "cancelled_in_amount",
                                f"{c.contract_id}: отменённые работы на {fmt(row.cancelled_price_kop)} "
                                f"входят в сумму договора; остаток по договору больше остатка по работам "
                                f"на {fmt(row.delta_kop)} — нужно допсоглашение"))
        if unallocated:
            issues.append(Issue("warning", f"contracts.csv:{c.row}", "unallocated",
                                f"{c.contract_id}: не разнесено по работам {fmt(unallocated)} "
                                f"({', '.join(pid for pid, _ in alloc.unallocated)})"))
        if row.overpaid_kop:
            issues.append(Issue("warning", f"contracts.csv:{c.row}", "overpaid",
                                f"{c.contract_id}: переплата относительно суммы договора "
                                f"{fmt(row.overpaid_kop)}"))
        for note in alloc.notes:
            issues.append(Issue("warning", f"contracts.csv:{c.row}", "allocation", f"{c.contract_id}: {note}"))

    return Report(as_of=on, strategy=strategy, contracts=rows, issues=issues)
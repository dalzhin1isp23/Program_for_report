from __future__ import annotations

import csv
import json
from pathlib import Path

from .money import fmt, fmt_csv, fmt_json
from .report import Report

STATE_RU = {"done": "принята", "planned": "план", "cancelled": "отменена"}


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _table(headers, rows, aligns) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def line(cells):
        return "  ".join(c.rjust(widths[i]) if aligns[i] == "r" else c.ljust(widths[i])
                         for i, c in enumerate(cells)).rstrip()

    return "\n".join([line(headers), "  ".join("-" * w for w in widths)] + [line(r) for r in rows])


def to_console(rep: Report) -> str:
    out = [f"Отчёт на {rep.as_of:%d.%m.%Y}   разнесение непривязанных платежей: {rep.strategy}", ""]
    wrows = [[w.contract_id, w.work_id, _cut(w.title, 26), STATE_RU[w.state],
              fmt(w.price_kop), fmt(w.paid_kop), fmt(w.remaining_kop)] for w in rep.works]
    total = [sum(w.price_kop for w in rep.works), sum(w.paid_kop for w in rep.works),
             sum(w.remaining_kop for w in rep.works)]
    wrows.append(["ИТОГО", "", "", "", *[fmt(x) for x in total]])
    out += ["РАБОТЫ", _table(["Договор", "Работа", "Название", "Статус", "Цена", "Выплачено", "Осталось"],
                             wrows, "llllrrr"), ""]

    crows = [[c.contract_id, _cut(c.contractor, 22), fmt(c.amount_kop), fmt(c.paid_kop),
              fmt(c.remaining_by_amount_kop), fmt(c.remaining_by_works_kop)] for c in rep.contracts]
    crows.append(["ИТОГО", "", *[fmt(sum(getattr(c, f) for c in rep.contracts))
                                for f in ("amount_kop", "paid_kop",
                                          "remaining_by_amount_kop", "remaining_by_works_kop")]])
    out += ["ДОГОВОРЫ",
            _table(["Договор", "Подрядчик", "Сумма", "Выплачено", "Осталось/договор", "Осталось/работы"],
                   crows, "llrrrr"), ""]

    notes = []
    for c in rep.contracts:
        if c.queued_kop:
            notes.append(f"{c.contract_id}: в очереди на оплату {fmt(c.queued_kop)} — деньги не ушли")
        if c.refunded_kop:
            notes.append(f"{c.contract_id}: возвращено подрядчиком {fmt(c.refunded_kop)}")
        if c.unallocated_kop:
            notes.append(f"{c.contract_id}: не разнесено по работам {fmt(c.unallocated_kop)}")
        if c.delta_kop:
            notes.append(f"{c.contract_id}: разница договор/работы {fmt(c.delta_kop)}")
    for i in rep.issues:
        if i.severity != "info":
            notes.append(f"[{i.severity}] {i.source} {i.message}")
    for i in rep.issues:
        if i.severity == "info":
            notes.append(f"[инфо] {i.message}")
    if notes:
        out += ["СВЕРКА И ВНИМАНИЕ", *[f"- {n}" for n in notes]]
    return "\n".join(out)


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


def to_csv(rep: Report, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    m = fmt_csv
    _write(out_dir / "works_report.csv",
           ["contract_id", "work_id", "title", "state_as_of", "status_export", "price",
            "paid", "remaining", "paid_linked", "paid_from_pool", "queued", "refunded"],
           [[w.contract_id, w.work_id, w.title, w.state, w.status_export, m(w.price_kop),
             m(w.paid_kop), m(w.remaining_kop), m(w.linked_kop), m(w.pool_kop),
             m(w.queued_kop), m(w.refunded_kop)] for w in rep.works])
    _write(out_dir / "contracts_report.csv",
           ["contract_id", "contractor", "amount", "works_price", "active_price",
            "gross_paid", "refunded", "paid", "queued", "allocated", "unallocated",
            "remaining_by_amount", "remaining_by_works", "overpaid", "delta"],
           [[c.contract_id, c.contractor, *[m(getattr(c, f)) for f in (
               "amount_kop", "works_price_kop", "active_price_kop", "gross_paid_kop",
               "refunded_kop", "paid_kop", "queued_kop", "allocated_kop", "unallocated_kop",
               "remaining_by_amount_kop", "remaining_by_works_kop", "overpaid_kop", "delta_kop")]]
            for c in rep.contracts])
    _write(out_dir / "allocations.csv",
           ["payment_id", "contract_id", "work_id", "kind", "amount"],
           [[x.payment_id, w.contract_id, x.work_id, x.kind, m(x.amount_kop)]
            for c in rep.contracts for w in c.works for x in w.pieces]
           + [[pid, c.contract_id, "", "unallocated", m(a)]
              for c in rep.contracts for pid, a in c.unallocated_detail])
    _write(out_dir / "issues.csv", ["severity", "source", "code", "message"],
           [[i.severity, i.source, i.code, i.message] for i in rep.issues])


def to_json(rep: Report, path: Path) -> None:
    def money(k):
        return fmt_json(k)

    payload = {
        "as_of": rep.as_of.isoformat(),
        "allocation": rep.strategy,
        "money": "строки, рубли, ровно 2 знака, разделитель точка",
        "contracts": [{
            "contract_id": c.contract_id, "contractor": c.contractor,
            **{f[:-4]: money(getattr(c, f)) for f in (
                "amount_kop", "works_price_kop", "active_price_kop", "gross_paid_kop",
                "refunded_kop", "paid_kop", "queued_kop", "allocated_kop", "unallocated_kop",
                "remaining_by_amount_kop", "remaining_by_works_kop", "overpaid_kop", "delta_kop")},
            "works": [{
                "work_id": w.work_id, "title": w.title, "state": w.state,
                "status_export": w.status_export,
                **{f[:-4]: money(getattr(w, f)) for f in (
                    "price_kop", "paid_kop", "remaining_kop", "linked_kop",
                    "pool_kop", "queued_kop", "refunded_kop")},
                "pieces": [{"payment_id": x.payment_id, "kind": x.kind,
                            "amount": money(x.amount_kop)} for x in w.pieces],
            } for w in c.works],
        } for c in rep.contracts],
        "issues": [vars(i) for i in rep.issues],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
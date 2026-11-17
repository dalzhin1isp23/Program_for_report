"""Чтение CSV. Битые строки не теряются молча: либо исключаем с error, либо warning."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .models import Contract, Issue, Payment, Work
from .money import MoneyError, fmt, parse_kop

REQUIRED = {
    "contracts.csv": ["contract_id", "contractor", "amount", "signed_at"],
    "works.csv": ["work_id", "contract_id", "title", "price", "status",
                  "completed_at", "cancelled_at"],
    "payments.csv": ["payment_id", "contract_id", "work_id", "purpose", "amount",
                     "status", "created_at", "paid_at", "refunded_at"],
}
WORK_STATUSES = {"planned", "done", "cancelled"}
PAYMENT_STATUSES = {"paid", "queued", "refunded"}
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y")


class FatalData(Exception):
    pass


class DateError(ValueError):
    pass


def parse_date(raw: str | None) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    s = s.replace("T", " ").split(" ")[0]   # время отбрасываем: отчёт суточный
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise DateError(f"не похоже на дату: {raw!r}")


@dataclass
class LoadResult:
    contracts: list[Contract] = field(default_factory=list)
    works: list[Work] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


class _Row:
    """Аккумулятор проблем одной строки."""

    def __init__(self, source: str, issues: list[Issue]):
        self.source, self.issues, self.broken = source, issues, False

    def fail(self, code: str, msg: str) -> None:
        self.issues.append(Issue("error", self.source, code, msg))
        self.broken = True

    def warn(self, code: str, msg: str) -> None:
        self.issues.append(Issue("warning", self.source, code, msg))

    def money(self, raw: str | None, name: str) -> int:
        try:
            return parse_kop(raw)
        except MoneyError as exc:
            self.fail("bad_money", f"{name}: {exc}")
            return 0

    def date(self, raw: str | None, name: str, critical: bool = False) -> date | None:
        try:
            return parse_date(raw)
        except DateError as exc:
            if critical:
                self.fail("bad_date", f"{name}: {exc}")
            else:
                self.warn("bad_date", f"{name}: {exc}")
            return None


def _read(path: Path, columns: list[str]) -> list[tuple[int, dict]]:
    if not path.exists():
        raise FatalData(f"нет файла {path}")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.readline()
        fh.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(fh, delimiter=delim)
        header = [(h or "").strip() for h in (reader.fieldnames or [])]
        missing = [c for c in columns if c not in header]
        if missing:
            raise FatalData(f"{path.name}: нет колонок {', '.join(missing)}")
        rows = []
        for raw in reader:
            clean = {(k or "").strip(): (v or "") for k, v in raw.items() if k}
            rows.append((reader.line_num, clean))
        return rows


def load(data_dir: Path) -> LoadResult:
    res = LoadResult()
    issues = res.issues
    tables = {name: _read(data_dir / name, cols) for name, cols in REQUIRED.items()}

    seen: set[str] = set()
    for line, r in tables["contracts.csv"]:
        row = _Row(f"contracts.csv:{line}", issues)
        cid = r["contract_id"].strip()
        if not cid:
            row.fail("empty_id", "пустой contract_id")
        elif cid in seen:
            row.fail("duplicate_id", f"дубль договора {cid}")
        amount = row.money(r["amount"], "amount")
        if amount <= 0 and not row.broken:
            row.fail("bad_amount", f"{cid}: сумма договора {fmt(amount)} должна быть > 0")
        signed = row.date(r["signed_at"], "signed_at")
        if row.broken:
            continue
        seen.add(cid)
        res.contracts.append(Contract(cid, r["contractor"].strip(), amount, signed, line))

    contracts_by_id = {c.contract_id: c for c in res.contracts}
    works_by_id: dict[str, Work] = {}
    for line, r in tables["works.csv"]:
        row = _Row(f"works.csv:{line}", issues)
        wid, cid = r["work_id"].strip(), r["contract_id"].strip()
        if not wid:
            row.fail("empty_id", "пустой work_id")
        elif wid in works_by_id:
            row.fail("duplicate_id", f"дубль работы {wid}")
        if cid not in contracts_by_id:
            row.fail("unknown_contract", f"{wid}: договор {cid!r} не найден")
        price = row.money(r["price"], "price")
        if price < 0 and not row.broken:
            row.fail("bad_price", f"{wid}: отрицательная цена")
        status = r["status"].strip()
        if status not in WORK_STATUSES:
            row.fail("bad_status", f"{wid}: неизвестный статус {status!r}")
        done_at = row.date(r["completed_at"], "completed_at", critical=True)
        canc_at = row.date(r["cancelled_at"], "cancelled_at", critical=True)
        if status == "done" and done_at is None:
            row.warn("no_event_date", f"{wid}: done без completed_at — статус берём из выгрузки")
        if status == "cancelled" and canc_at is None:
            row.warn("no_event_date", f"{wid}: cancelled без cancelled_at — статус берём из выгрузки")
        if done_at and canc_at:
            row.warn("both_dates", f"{wid}: заполнены и completed_at, и cancelled_at")
        if row.broken:
            continue
        work = Work(wid, cid, r["title"].strip(), price, status, done_at, canc_at, line)
        works_by_id[wid] = work
        res.works.append(work)

    seen_p: set[str] = set()
    for line, r in tables["payments.csv"]:
        row = _Row(f"payments.csv:{line}", issues)
        pid, cid = r["payment_id"].strip(), r["contract_id"].strip()
        if not pid:
            row.fail("empty_id", "пустой payment_id")
        elif pid in seen_p:
            row.fail("duplicate_id", f"дубль платежа {pid}")
        if cid not in contracts_by_id:
            row.fail("unknown_contract", f"{pid}: договор {cid!r} не найден")
        amount = row.money(r["amount"], "amount")
        if amount <= 0 and not row.broken:
            row.fail("bad_amount", f"{pid}: сумма платежа {fmt(amount)} должна быть > 0")
        status = r["status"].strip()
        if status not in PAYMENT_STATUSES:
            row.fail("bad_status", f"{pid}: неизвестный статус {status!r}")
        created = row.date(r["created_at"], "created_at")
        paid_at = row.date(r["paid_at"], "paid_at", critical=True)
        refunded_at = row.date(r["refunded_at"], "refunded_at", critical=True)

        wid = r["work_id"].strip() or None
        if wid:
            work = works_by_id.get(wid)
            if work is None:
                row.warn("unknown_work", f"{pid}: работа {wid} не найдена — считаем платёж непривязанным")
                wid = None
            elif work.contract_id != cid:
                row.warn("foreign_work",
                         f"{pid}: работа {wid} из договора {work.contract_id}, платёж по {cid} — считаем непривязанным")
                wid = None

        # Согласованность статуса и дат. Правило: факт = дата, статус — подсказка.
        if status == "paid" and paid_at is None:
            row.warn("paid_without_date", f"{pid}: paid без paid_at — в «выплачено» не попадёт")
        if status == "queued" and paid_at is not None:
            row.warn("queued_with_date", f"{pid}: queued, но есть paid_at {paid_at} — считаем оплаченным по дате")
        if status == "refunded":
            if paid_at is None:
                row.warn("refund_without_payment", f"{pid}: refunded без paid_at")
            if refunded_at is None:
                row.warn("refund_without_date", f"{pid}: refunded без refunded_at — считаем невозвращённым")
        if paid_at and refunded_at and refunded_at < paid_at:
            row.warn("refund_before_payment", f"{pid}: возврат {refunded_at} раньше оплаты {paid_at}")
        if row.broken:
            continue
        seen_p.add(pid)
        res.payments.append(Payment(pid, cid, wid, r["purpose"].strip(), amount,
                                    status, created, paid_at, refunded_at, line))

    return res
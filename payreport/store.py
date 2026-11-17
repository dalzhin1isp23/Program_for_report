"""Добавление, правка и удаление записей через интерфейс.

Правила:
  * исходная выгрузка неприкосновенна — править и удалять можно только записи,
    добавленные здесь же (они помечены в журнале);
  * каждое действие пишется в data/_journal.jsonl, поэтому его можно отменить;
  * суммы пишутся в копейках через fmt_json, даты — ISO, как в исходных файлах;
  * запись файла атомарная, перед перезаписью делается .bak;
  * все обращения к данным идут под одним замком — сервер многопоточный.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .loading import PAYMENT_STATUSES, WORK_STATUSES, DateError, parse_date
from .money import MoneyError, fmt_json, parse_kop

LOCK = threading.RLock()

FILES = {"contract": "contracts.csv", "work": "works.csv", "payment": "payments.csv"}
COLUMNS = {
    "contract": ["contract_id", "contractor", "amount", "signed_at"],
    "work": ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"],
    "payment": ["payment_id", "contract_id", "work_id", "purpose", "amount", "status",
                "created_at", "paid_at", "refunded_at"],
}
ID_FIELD = {"contract": "contract_id", "work": "work_id", "payment": "payment_id"}
LABEL = {"contract": "Договор", "work": "Работа", "payment": "Платёж"}
JOURNAL = "_journal.jsonl"

_ID_RE = re.compile(r"^[\w.\-/]+$", re.UNICODE)


class StoreError(ValueError):
    """Понятная бухгалтеру причина отказа."""


# ---------------------------------------------------------------- файлы

def _kind(kind: str) -> str:
    if kind not in FILES:
        raise StoreError(f"Неизвестный тип записи: {kind}")
    return kind


def _delimiter(path: Path) -> str:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.readline()
    return ";" if sample.count(";") > sample.count(",") else ","


def _read(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        raise StoreError(f"нет файла {path.name}")
    delim = _delimiter(path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh, delimiter=delim)
                if any((c or "").strip() for c in r)]
    if not rows:
        raise StoreError(f"{path.name} пуст — нет даже заголовка")
    header = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        r = list(r) + [""] * max(0, len(header) - len(r))
        out.append({h: (r[i] or "").strip() for i, h in enumerate(header)})
    return header, out


def _tables(data_dir: Path) -> dict[str, list[dict]]:
    return {k: _read(data_dir / name)[1] for k, name in FILES.items()}


def _text(header: list[str], rows: list[dict], delim: str) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=delim, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(h, "") for h in header])
    return buf.getvalue()


def _atomic_write(path: Path, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(payload)
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise StoreError(f"{path.name} занят другой программой. Закройте его и повторите.")
                time.sleep(0.06)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _append_row(path: Path, header: list[str], row: dict) -> None:
    delim = _delimiter(path)
    tail = b""
    with path.open("rb") as fh:
        fh.seek(0, 2)
        if fh.tell():
            fh.seek(-1, 2)
            tail = fh.read(1)
    line = _text([], [row], delim) if False else None  # noqa — только чтобы явно не путать форматы
    buf = io.StringIO(newline="")
    csv.writer(buf, delimiter=delim, lineterminator="\n").writerow([row.get(h, "") for h in header])
    with path.open("a", encoding="utf-8", newline="") as fh:
        if tail and tail not in (b"\n", b"\r"):
            fh.write("\n")
        fh.write(buf.getvalue())


def _rewrite(path: Path, header: list[str], rows: list[dict]) -> None:
    delim = _delimiter(path)
    shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    _atomic_write(path, _text(header, rows, delim))


# ---------------------------------------------------------------- журнал

def _journal(data_dir: Path, entry: dict) -> None:
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
    with (data_dir / JOURNAL).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_journal(data_dir: Path) -> list[dict]:
    p = data_dir / JOURNAL
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def manual_index(data_dir: Path) -> dict[str, list[str]]:
    """Какие записи созданы через интерфейс — их можно править и удалять."""
    with LOCK:
        state: dict[tuple, bool] = {}
        for e in read_journal(data_dir):
            key = (e.get("kind"), e.get("id"))
            if e.get("action") in ("add", "update"):
                state[key] = True
            elif e.get("action") == "delete":
                state[key] = False
        return {k: sorted(i for (kk, i), live in state.items() if kk == k and live) for k in FILES}


# ---------------------------------------------------------------- разбор полей

def _plain(fields: dict, name: str, label: str, required: bool = True) -> str:
    v = str(fields.get(name, "") or "").strip()
    if required and not v:
        raise StoreError(f"{label}: обязательное поле")
    if any(ch in v for ch in ("\r", "\n", "\t")):
        raise StoreError(f"{label}: уберите переносы строк и табуляцию")
    return v


def _ident(fields: dict, name: str, label: str) -> str:
    v = _plain(fields, name, label)
    if not _ID_RE.match(v):
        raise StoreError(f"{label}: допустимы буквы, цифры, дефис, точка, подчёркивание — без пробелов")
    return v


def _money(fields: dict, name: str, label: str, positive: bool = True) -> str:
    raw = str(fields.get(name, "") or "").strip()
    if not raw:
        raise StoreError(f"{label}: обязательное поле")
    try:
        kop = parse_kop(raw)
    except MoneyError as exc:
        raise StoreError(f"{label}: {exc}") from exc
    if positive and kop <= 0:
        raise StoreError(f"{label}: сумма должна быть больше нуля")
    if kop < 0:
        raise StoreError(f"{label}: сумма не может быть отрицательной")
    return fmt_json(kop)


def _date(fields: dict, name: str, label: str, required: bool = False) -> str:
    raw = str(fields.get(name, "") or "").strip()
    if not raw:
        if required:
            raise StoreError(f"{label}: обязательное поле")
        return ""
    try:
        d = parse_date(raw)
    except DateError as exc:
        raise StoreError(f"{label}: {exc}") from exc
    if d is None:
        if required:
            raise StoreError(f"{label}: обязательное поле")
        return ""
    if not 1900 <= d.year <= 2200:
        raise StoreError(f"{label}: проверьте год — {d.year}")
    return d.isoformat()


def validate(data_dir: Path, kind: str, fields: dict, editing: str | None = None) -> dict:
    """Проверяет запись и приводит её к формату исходных CSV."""
    _kind(kind)
    t = _tables(data_dir)

    if kind == "contract":
        cid = _ident(fields, "contract_id", "Номер договора")
        if cid != editing and any(c["contract_id"] == cid for c in t["contract"]):
            raise StoreError(f"Договор {cid} уже есть в выгрузке")
        return {
            "contract_id": cid,
            "contractor": _plain(fields, "contractor", "Подрядчик"),
            "amount": _money(fields, "amount", "Сумма договора"),
            "signed_at": _date(fields, "signed_at", "Дата подписания", required=True),
        }

    if kind == "work":
        wid = _ident(fields, "work_id", "Номер работы")
        if wid != editing and any(w["work_id"] == wid for w in t["work"]):
            raise StoreError(f"Работа {wid} уже есть в выгрузке")
        cid = _plain(fields, "contract_id", "Договор")
        if not any(c["contract_id"] == cid for c in t["contract"]):
            raise StoreError(f"Договор {cid} не найден — сначала добавьте договор")
        status = _plain(fields, "status", "Статус")
        if status not in WORK_STATUSES:
            raise StoreError(f"Неизвестный статус работы: {status}")
        row = {
            "work_id": wid, "contract_id": cid,
            "title": _plain(fields, "title", "Название"),
            "price": _money(fields, "price", "Цена", positive=False),
            "status": status, "completed_at": "", "cancelled_at": "",
        }
        if status == "done":
            row["completed_at"] = _date(fields, "completed_at", "Когда принята", required=True)
        if status == "cancelled":
            row["cancelled_at"] = _date(fields, "cancelled_at", "Когда отменена", required=True)
        if editing:
            old = next((w for w in t["work"] if w["work_id"] == editing), None)
            if old and old["contract_id"] != cid and any(p["work_id"] == editing for p in t["payment"]):
                raise StoreError("К работе привязаны платежи — перенести её в другой договор нельзя")
        return row

    pid = _ident(fields, "payment_id", "Номер платежа")
    if pid != editing and any(p["payment_id"] == pid for p in t["payment"]):
        raise StoreError(f"Платёж {pid} уже есть в выгрузке")
    cid = _plain(fields, "contract_id", "Договор")
    if not any(c["contract_id"] == cid for c in t["contract"]):
        raise StoreError(f"Договор {cid} не найден")
    wid = _plain(fields, "work_id", "Работа", required=False)
    if wid:
        work = next((w for w in t["work"] if w["work_id"] == wid), None)
        if work is None:
            raise StoreError(f"Работа {wid} не найдена")
        if work["contract_id"] != cid:
            raise StoreError(f"Работа {wid} относится к договору {work['contract_id']}, а не к {cid}")
    status = _plain(fields, "status", "Статус")
    if status not in PAYMENT_STATUSES:
        raise StoreError(f"Неизвестный статус платежа: {status}")
    row = {
        "payment_id": pid, "contract_id": cid, "work_id": wid,
        "purpose": _plain(fields, "purpose", "Назначение"),
        "amount": _money(fields, "amount", "Сумма"),
        "status": status,
        "created_at": _date(fields, "created_at", "Когда заведён", required=True),
        "paid_at": "", "refunded_at": "",
    }
    if status in ("paid", "refunded"):
        row["paid_at"] = _date(fields, "paid_at", "Когда деньги ушли", required=True)
    if status == "refunded":
        row["refunded_at"] = _date(fields, "refunded_at", "Когда вернули", required=True)
        if row["refunded_at"] < row["paid_at"]:
            raise StoreError("Дата возврата раньше даты оплаты")
    return row


def _guard_manual(data_dir: Path, kind: str, rid: str) -> None:
    if rid not in manual_index(data_dir)[kind]:
        raise StoreError("Править и удалять можно только записи, добавленные через интерфейс. "
                         "Исходная выгрузка остаётся неприкосновенной.")


def _header_for(data_dir: Path, kind: str) -> tuple[Path, list[str]]:
    path = data_dir / FILES[kind]
    header, _ = _read(path)
    missing = [c for c in COLUMNS[kind] if c not in header]
    if missing:
        raise StoreError(f"{path.name}: нет колонок {', '.join(missing)}")
    return path, header


# ---------------------------------------------------------------- действия

def add_record(data_dir: Path, kind: str, fields: dict) -> dict:
    _kind(kind)
    with LOCK:
        path, header = _header_for(data_dir, kind)
        row = validate(data_dir, kind, fields)
        _append_row(path, header, row)
        rid = row[ID_FIELD[kind]]
        _journal(data_dir, {"action": "add", "kind": kind, "id": rid, "row": row})
        return {"kind": kind, "id": rid, "row": row,
                "message": f"{LABEL[kind]} {rid} добавлен в {FILES[kind]}"}


def update_record(data_dir: Path, kind: str, rid: str, fields: dict) -> dict:
    _kind(kind)
    with LOCK:
        _guard_manual(data_dir, kind, rid)
        path, header = _header_for(data_dir, kind)
        _, rows = _read(path)
        idf = ID_FIELD[kind]
        before = next((r for r in rows if r[idf] == rid), None)
        if before is None:
            raise StoreError(f"Запись {rid} не найдена")
        row = validate(data_dir, kind, {**fields, idf: rid}, editing=rid)
        _rewrite(path, header, [row if r[idf] == rid else r for r in rows])
        _journal(data_dir, {"action": "update", "kind": kind, "id": rid, "row": row, "before": before})
        return {"kind": kind, "id": rid, "row": row, "message": f"{LABEL[kind]} {rid} изменён"}


def delete_record(data_dir: Path, kind: str, rid: str) -> dict:
    _kind(kind)
    with LOCK:
        _guard_manual(data_dir, kind, rid)
        t = _tables(data_dir)
        path, header = _header_for(data_dir, kind)
        _, rows = _read(path)
        idf = ID_FIELD[kind]
        target = next((r for r in rows if r[idf] == rid), None)
        if target is None:
            raise StoreError(f"Запись {rid} не найдена")
        if kind == "contract":
            if any(w["contract_id"] == rid for w in t["work"]):
                raise StoreError("По договору есть работы — сначала удалите их")
            if any(p["contract_id"] == rid for p in t["payment"]):
                raise StoreError("По договору есть платежи — сначала удалите их")
        if kind == "work" and any(p["work_id"] == rid for p in t["payment"]):
            raise StoreError("К работе привязаны платежи — сначала удалите их или снимите привязку")
        _rewrite(path, header, [r for r in rows if r[idf] != rid])
        _journal(data_dir, {"action": "delete", "kind": kind, "id": rid, "row": target})
        return {"kind": kind, "id": rid, "row": target, "message": f"{LABEL[kind]} {rid} удалён"}


def undo_last(data_dir: Path, kind: str, rid: str) -> dict:
    """Отменяет последнее действие по записи: добавление, правку или удаление."""
    _kind(kind)
    with LOCK:
        entries = [e for e in read_journal(data_dir) if e.get("kind") == kind and e.get("id") == rid]
        if not entries:
            raise StoreError("Для этой записи нечего отменять")
        last = entries[-1]
        action = last.get("action")
        path, header = _header_for(data_dir, kind)
        _, rows = _read(path)
        idf = ID_FIELD[kind]

        if action == "add":
            delete_record(data_dir, kind, rid)
            return {"kind": kind, "id": rid, "undone": "add",
                    "message": f"Добавление {rid} отменено"}

        if action == "delete":
            row = last.get("row") or {}
            if any(r[idf] == rid for r in rows):
                raise StoreError(f"{rid} уже снова есть в файле")
            _append_row(path, header, row)
            _journal(data_dir, {"action": "add", "kind": kind, "id": rid, "row": row})
            return {"kind": kind, "id": rid, "undone": "delete",
                    "message": f"{rid} возвращён в выгрузку"}

        before = last.get("before") or {}
        if not any(r[idf] == rid for r in rows):
            raise StoreError(f"Запись {rid} не найдена")
        _rewrite(path, header, [before if r[idf] == rid else r for r in rows])
        _journal(data_dir, {"action": "update", "kind": kind, "id": rid,
                            "row": before, "before": last.get("row") or {}})
        return {"kind": kind, "id": rid, "undone": "update",
                "message": f"Правка {rid} отменена"}
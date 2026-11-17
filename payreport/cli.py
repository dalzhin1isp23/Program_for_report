# cli.py
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .loading import FatalData, load, parse_date
from .render import to_console, to_csv, to_json
from .report import InvariantError, build_report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="payreport",
                                description="Выплачено / осталось по работам и договорам на дату")
    p.add_argument("--date", required=True, help="дата отчёта, напр. 2026-11-10 или 10.11.2026")
    p.add_argument("--data", default="data", help="папка с CSV (по умолчанию data)")
    p.add_argument("--out", default="out", help="куда писать файлы (по умолчанию out)")
    p.add_argument("--allocation", default="fifo", choices=["fifo", "proportional"],
                   help="как разносить платежи без привязки к работе")
    p.add_argument("--format", default="console,csv,json")
    args = p.parse_args(argv)

    try:
        on = parse_date(args.date)
    except Exception:
        on = None
    if on is None:
        print(f"Не разобрал дату: {args.date!r}", file=sys.stderr)
        return 2

    try:
        data = load(Path(args.data))
        rep = build_report(data, on, args.allocation)
    except FatalData as exc:
        print(f"Данные: {exc}", file=sys.stderr)
        return 2
    except InvariantError as exc:
        print(f"ВНУТРЕННЯЯ ПРОВЕРКА НЕ ПРОШЛА: {exc}", file=sys.stderr)
        return 2

    formats = {f.strip() for f in args.format.split(",") if f.strip()}
    out = Path(args.out)
    if "console" in formats:
        print(to_console(rep))
    if "csv" in formats:
        to_csv(rep, out)
    if "json" in formats:
        to_json(rep, out / "report.json")
    if formats - {"console"}:
        print(f"\nФайлы: {out.resolve()}")
    return 1 if data.has_errors else 0
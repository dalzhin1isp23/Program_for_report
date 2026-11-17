"""Локальный сервер: отдаёт интерфейс, считает отчёт и принимает новые записи.

Деньги считает только Python (целые копейки). Сервер слушает 127.0.0.1 и отдаёт
ровно один HTML-файл. Порт занимается монопольно: если на нём уже висит прежний
процесс, мы не «подселяемся» рядом, а берём следующий свободный и печатаем его.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import store
from .asof import payment_effect
from .loading import DateError, FatalData, load, parse_date
from .money import fmt_json
from .render import to_csv, to_json
from .report import InvariantError, build_report
from .store import StoreError, add_record, delete_record, manual_index, undo_last, update_record

API_VERSION = 3
UI_FILE = Path(__file__).parent / "ui" / "index.html"
EXPORTS = {
    "works": ("works_report.csv", "text/csv; charset=utf-8"),
    "contracts": ("contracts_report.csv", "text/csv; charset=utf-8"),
    "allocations": ("allocations.csv", "text/csv; charset=utf-8"),
    "issues": ("issues.csv", "text/csv; charset=utf-8"),
    "json": ("report.json", "application/json; charset=utf-8"),
}
STRATEGY_LABEL = {"fifo": "сначала принятые работы", "proportional": "пропорционально цене"}
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#0B1120"/>'
    '<path d="M7 21h18" stroke="#00E58F" stroke-width="3" stroke-linecap="round"/>'
    '<path d="M7 15h11" stroke="#2FD8FF" stroke-width="3" stroke-linecap="round"/>'
    '<path d="M7 9h6" stroke="#FFB020" stroke-width="3" stroke-linecap="round"/></svg>'
)
MISSING_UI = """<!doctype html><meta charset="utf-8">
<body style="font:16px/1.6 system-ui;background:#0B1120;color:#E8EEFF;padding:48px">
<h1>Не найден файл интерфейса</h1>
<p>Сохраните интерфейс в <code>payreport/ui/index.html</code> и обновите страницу.</p>
<p>Командный отчёт работает независимо: <code>python -m payreport --date 2026-11-10</code></p>
</body>"""


class HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Server(ThreadingHTTPServer):
    """Монопольный порт — нельзя случайно оказаться рядом со старым процессом."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


def pm(part: int, whole: int) -> int:
    """Доля в промилле, целыми. Только для ширины полосок в интерфейсе."""
    if whole <= 0:
        return 0
    return max(0, min(1000, part * 1000 // whole))


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def build_payload(data_dir: Path, on: date, strategy: str, server_info: dict | None = None) -> dict:
    with store.LOCK:
        data = load(data_dir)
        rep = build_report(data, on, strategy)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            to_json(rep, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        manual = manual_index(data_dir)

    raw_works = {w.work_id: w for w in data.works}
    raw_contracts = {c.contract_id: c for c in data.contracts}
    by_id = {c.contract_id: c for c in rep.contracts}

    for c in payload["contracts"]:
        row = by_id[c["contract_id"]]
        raw = raw_contracts[c["contract_id"]]
        c["signed_at"] = _iso(raw.signed_at)
        c["cancelled_price"] = fmt_json(row.cancelled_price_kop)
        c["paid_on_cancelled"] = fmt_json(row.paid_on_cancelled_kop)
        c["progress_pm"] = pm(row.paid_kop, raw.amount_kop)
        c["allocated_pm"] = pm(row.allocated_kop, raw.amount_kop)
        c["unallocated_pm"] = pm(row.unallocated_kop, raw.amount_kop)
        c["unallocated_detail"] = [
            {"payment_id": pid, "amount": fmt_json(a)} for pid, a in row.unallocated_detail
        ]
        for w in c["works"]:
            wr = next(x for x in row.works if x.work_id == w["work_id"])
            rw = raw_works[w["work_id"]]
            w["completed_at"] = _iso(rw.completed_at)
            w["cancelled_at"] = _iso(rw.cancelled_at)
            w["progress_pm"] = pm(wr.paid_kop, rw.price_kop)
            w["linked_pm"] = pm(wr.linked_kop, rw.price_kop)
            w["pool_pm"] = pm(wr.pool_kop, rw.price_kop)
            w["share_pm"] = pm(wr.paid_kop, raw.amount_kop)

    max_amount = max((p.amount_kop for p in data.payments), default=0)
    payments = []
    for p in data.payments:
        eff = payment_effect(p, on)
        if eff.net_kop > 0:
            reason = f"деньги ушли {p.paid_at:%d.%m.%Y} — учтено"
        elif eff.state == "refunded":
            reason = f"подрядчик вернул {p.refunded_at:%d.%m.%Y} — из оплаты вычтено"
        elif p.paid_at is None:
            reason = ("в очереди на оплату — деньги не ушли" if p.status == "queued"
                      else "нет даты оплаты — в выплаченное не попало")
        else:
            reason = f"оплата {p.paid_at:%d.%m.%Y} позже даты отчёта — не учтено"
        payments.append({
            "payment_id": p.payment_id, "contract_id": p.contract_id, "work_id": p.work_id,
            "purpose": p.purpose, "amount": fmt_json(p.amount_kop), "status": p.status,
            "created_at": _iso(p.created_at), "paid_at": _iso(p.paid_at),
            "refunded_at": _iso(p.refunded_at), "effect_state": eff.state,
            "gross": fmt_json(eff.gross_kop), "refunded": fmt_json(eff.refunded_kop),
            "net": fmt_json(eff.net_kop), "queued": fmt_json(eff.queued_kop),
            "counted": eff.net_kop > 0, "reason": reason,
            "size_pm": pm(p.amount_kop, max_amount),
        })

    def total(field: str) -> str:
        return fmt_json(sum(getattr(c, field) for c in rep.contracts))

    payload["payments"] = payments
    payload["manual"] = manual
    payload["totals"] = {
        "amount": total("amount_kop"), "paid": total("paid_kop"),
        "gross_paid": total("gross_paid_kop"), "refunded": total("refunded_kop"),
        "queued": total("queued_kop"), "unallocated": total("unallocated_kop"),
        "overpaid": total("overpaid_kop"), "delta": total("delta_kop"),
        "remaining_by_amount": total("remaining_by_amount_kop"),
        "remaining_by_works": total("remaining_by_works_kop"),
    }
    payload["meta"] = {
        "api_version": API_VERSION,
        "server": server_info or {},
        "data_dir": str(data_dir),
        "strategy_label": STRATEGY_LABEL.get(strategy, strategy),
        "strategies": [{"id": k, "label": v} for k, v in STRATEGY_LABEL.items()],
        "contracts_count": len(rep.contracts),
        "works_count": len(rep.works),
        "payments_count": len(data.payments),
        "counted_payments": sum(1 for p in payments if p["counted"]),
        "manual_count": sum(len(v) for v in manual.values()),
        "has_errors": data.has_errors,
        "server_time": datetime.now().strftime("%H:%M:%S"),
    }
    return payload


def _params(qs: str) -> tuple[date, str, str]:
    q = parse_qs(qs)
    raw_date = (q.get("date") or [""])[0].strip()
    try:
        on = parse_date(raw_date) if raw_date else date.today()
    except DateError as exc:
        raise HttpError(400, f"Не понял дату: {exc}") from exc
    if on is None:
        raise HttpError(400, "Не понял дату отчёта. Пример: 2026-11-10")
    strategy = (q.get("allocation") or ["fifo"])[0]
    if strategy not in STRATEGY_LABEL:
        raise HttpError(400, f"Неизвестное правило разнесения: {strategy}")
    return on, strategy, (q.get("kind") or ["works"])[0]


class Handler(BaseHTTPRequestHandler):
    server_version = f"PayReport/{API_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    @property
    def info(self) -> dict:
        return getattr(self.server, "info", {})

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-PayReport-API", str(API_VERSION))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, code: int, message: str):
        if code >= 500:
            print(f"  [ошибка] {message}", file=sys.stderr)
        self._json(code, {"error": message})

    # ------------------------------------------------------------ чтение
    def do_GET(self):
        url = urlparse(self.path)
        path, data_dir = url.path, self.server.data_dir
        try:
            if path in ("/", "/index.html"):
                body = UI_FILE.read_bytes() if UI_FILE.exists() else MISSING_UI.encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            if path == "/favicon.svg":
                self._send(200, FAVICON.encode("utf-8"), "image/svg+xml")
                return
            if path == "/api/health":
                with store.LOCK:
                    manual = manual_index(data_dir)
                self._json(200, {"ok": True, "api_version": API_VERSION, **self.info,
                                 "ui_present": UI_FILE.exists(),
                                 "manual": {k: len(v) for k, v in manual.items()}})
                return
            if path == "/api/report":
                on, strategy, _ = _params(url.query)
                self._json(200, build_payload(data_dir, on, strategy, self.info))
                return
            if path == "/api/export":
                on, strategy, kind = _params(url.query)
                if kind not in EXPORTS:
                    raise HttpError(400, f"Неизвестный вид выгрузки: {kind}")
                name, ctype = EXPORTS[kind]
                with store.LOCK:
                    rep = build_report(load(data_dir), on, strategy)
                    with tempfile.TemporaryDirectory() as tmp:
                        out = Path(tmp)
                        if kind == "json":
                            to_json(rep, out / name)
                        else:
                            to_csv(rep, out)
                        body = (out / name).read_bytes()
                stem, ext = name.rsplit(".", 1)
                fname = f"{stem}_{on.isoformat()}_{strategy}.{ext}"
                self._send(200, body, ctype,
                           {"Content-Disposition": f'attachment; filename="{fname}"'})
                return
            raise HttpError(404, "Нет такой страницы")
        except HttpError as exc:
            self._fail(exc.code, exc.message)
        except StoreError as exc:
            self._fail(400, str(exc))
        except FatalData as exc:
            self._fail(400, f"Проблема с файлами данных: {exc}")
        except InvariantError as exc:
            self._fail(500, f"Внутренняя проверка не прошла: {exc}")
        except Exception as exc:
            self._fail(500, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ запись
    def do_POST(self):
        url, data_dir = urlparse(self.path), self.server.data_dir
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise HttpError(400, "Пустой запрос")
            if length > 1_000_000:
                raise HttpError(413, "Слишком большой запрос")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise HttpError(400, "Не разобрал тело запроса") from exc
            if not isinstance(body, dict):
                raise HttpError(400, "Ожидался объект JSON")
            if url.path != "/api/record":
                raise HttpError(404, "Нет такого действия")

            action = body.get("action")
            kind = body.get("kind")
            rid = str(body.get("id") or "")
            fields = body.get("fields") or {}
            if not isinstance(fields, dict):
                raise HttpError(400, "Поля записи должны быть объектом")

            if action == "add":
                result = add_record(data_dir, kind, fields)
            elif action == "update":
                result = update_record(data_dir, kind, rid, fields)
            elif action == "delete":
                result = delete_record(data_dir, kind, rid)
            elif action == "undo":
                result = undo_last(data_dir, kind, rid)
            else:
                raise HttpError(400, f"Неизвестное действие: {action}")

            self._json(200, {"ok": True, **result})
        except HttpError as exc:
            self._fail(exc.code, exc.message)
        except StoreError as exc:
            self._fail(400, str(exc))
        except FatalData as exc:
            self._fail(400, f"Проблема с файлами данных: {exc}")
        except Exception as exc:
            self._fail(500, f"{type(exc).__name__}: {exc}")


def serve(data_dir: Path, port: int = 8765, open_browser: bool = True) -> int:
    httpd, busy = None, []
    for candidate in range(port, port + 40):
        try:
            httpd = Server(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            busy.append(candidate)
            continue
    if httpd is None:
        print("Не нашёл свободный порт. Задайте другой: python run.py --port 9100")
        return 2

    httpd.data_dir = data_dir
    httpd.info = {
        "pid": os.getpid(),
        "port": port,
        "started_at": datetime.now().strftime("%H:%M:%S"),
        "python": sys.version.split()[0],
        "executable": sys.executable,
    }
    url = f"http://127.0.0.1:{port}/"

    print()
    print("  Расчёты с подрядчиками — интерфейс запущен")
    print("  " + "─" * 52)
    print(f"  Откройте в браузере:  {url}")
    print(f"  Файлы данных:         {data_dir}")
    print(f"  Процесс:              PID {os.getpid()}, Python {sys.version.split()[0]}")
    print("  Дата по умолчанию:    2026-11-10")
    print("  Новые записи:         кнопка «Добавить», отмена — в уведомлении")
    print(f"  Журнал правок:        {data_dir / store.JOURNAL}")
    print("  Остановить:           Ctrl + C")
    if busy:
        print()
        print(f"  Занятые порты:        {', '.join(str(p) for p in busy)}")
        print("  Там, скорее всего, висят прежние запуски. Откройте адрес выше,")
        print("  а старые окна закройте. Список: netstat -ano | findstr :8765")
    print()
    if not UI_FILE.exists():
        print(f"  ВНИМАНИЕ: нет файла интерфейса {UI_FILE}")
        print("  Сохраните payreport/ui/index.html — пока откроется заглушка.")
        print()

    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Остановлено. Данные, журнал и отчёт остались на месте.")
    finally:
        httpd.server_close()
    return 0
#!/usr/bin/env python3
"""Одна команда: считает отчёт и открывает понятный интерфейс в браузере.

Перед запуском проверяет, не висит ли на порту прежний сервер, и показывает,
какой процесс его держит. Порт занимается монопольно, поэтому браузер всегда
попадает в этот сервер, а не в старый.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_FILES = ("contracts.csv", "works.csv", "payments.csv")


def port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def probe(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.6) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return None


def owner_pids(port: int) -> list[int]:
    """Кто держит порт — только для подсказки, ничего не убиваем сами."""
    pids: list[int] = []
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                                 text=True, timeout=6).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line.upper():
                    m = re.search(r"(\d+)\s*$", line.strip())
                    if m:
                        pids.append(int(m.group(1)))
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True,
                                 text=True, timeout=6).stdout
            pids = [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        pass
    return sorted(set(pids))


def describe_busy(port: int) -> None:
    health = probe(port)
    pids = owner_pids(port)
    who = ", ".join(str(p) for p in pids) if pids else "неизвестно"
    if health is None:
        print(f"  Порт {port} занят другой программой (PID: {who}).")
    else:
        ver = health.get("api_version")
        if ver:
            print(f"  Порт {port} держит наш сервер API {ver}, PID {health.get('pid', who)}.")
        else:
            print(f"  Порт {port} держит СТАРЫЙ сервер без API-версии (PID: {who}).")
    if pids:
        if os.name == "nt":
            print("  Закрыть его: " + " ; ".join(f"taskkill /PID {p} /F" for p in pids))
        else:
            print("  Закрыть его: " + " ; ".join(f"kill {p}" for p in pids))


def kill_pids(pids: list[int]) -> None:
    for p in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p), "/F"], check=False,
                               capture_output=True, text=True, timeout=10)
            else:
                os.kill(p, 15)
            print(f"  Процесс {p} остановлен.")
        except Exception as exc:
            print(f"  Не удалось остановить {p}: {exc}")


def main() -> int:
    if sys.version_info < (3, 10):
        print("Нужен Python 3.10 или новее. Сейчас: " + sys.version.split()[0])
        print("Интерпретатор: " + sys.executable)
        return 2

    p = argparse.ArgumentParser(description="Отчёт по расчётам с подрядчиками — интерфейс")
    p.add_argument("--data", default=str(ROOT / "data"), help="папка с CSV")
    p.add_argument("--port", type=int, default=8765, help="порт локального сервера")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    p.add_argument("--kill-stale", action="store_true",
                   help="остановить прежний сервер на этом порту (спросит подтверждение)")
    a = p.parse_args()

    print()
    print(f"  Python: {sys.version.split()[0]} — {sys.executable}")

    try:
        from payreport.webapp import serve
    except ImportError as exc:
        print("  Не найден пакет payreport рядом с run.py:", exc)
        print("  Проверьте, что папка payreport лежит в", ROOT)
        return 2

    data = Path(a.data)
    missing = [f for f in DATA_FILES if not (data / f).exists()]
    if missing:
        print("  В папке", data, "не хватает файлов:", ", ".join(missing))
        return 2

    if not port_free(a.port):
        print()
        describe_busy(a.port)
        if a.kill_stale:
            pids = owner_pids(a.port)
            if pids:
                answer = input(f"  Остановить PID {', '.join(map(str, pids))}? напишите yes: ").strip().lower()
                if answer in ("yes", "y", "да"):
                    kill_pids(pids)
                else:
                    print("  Не трогаю. Возьму следующий свободный порт.")
            else:
                print("  Не смог определить процесс — возьму следующий свободный порт.")
        else:
            print("  Возьму следующий свободный порт (или запустите с --kill-stale).")

    return serve(data, a.port, not a.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
"""Запуск сервиса.

    python -m servis                 — на localhost:8000, для проверки
    python -m servis --host 0.0.0.0  — принимать со всей сети

На рабочем сервере запускать как службу и закрывать HTTPS —
см. servis/README.md.
"""
from __future__ import annotations

import argparse


def main() -> int:
    cli = argparse.ArgumentParser(prog="servis", description=__doc__)
    cli.add_argument("--host", default="127.0.0.1")
    cli.add_argument("--port", type=int, default=8000)
    cli.add_argument("--reload", action="store_true", help="перезапуск при правке кода")
    args = cli.parse_args()

    import uvicorn

    uvicorn.run("servis.app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

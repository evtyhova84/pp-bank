"""Сквозной прогон: папка со счетами -> файл платёжек для клиент-банка.

    python konveyer.py --scheta "путь\к\папке" --payer spravochniki/platelshchik.json

Часть 2 распознаёт, часть 1 формирует файл. Счета с замечаниями по умолчанию
не выгружаются: сначала их смотрит человек.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "1-vygruzka-v-bank"))
sys.path.insert(0, str(BASE / "2-raspoznavanie"))

from generator.cli import DEFAULT_OUT_DIR, DEFAULT_REGISTRY, load_profile  # noqa: E402
from generator.export import ExportError, Exporter  # noqa: E402
from generator.models import Invoice, Payer, parse_date  # noqa: E402
from generator.numbering import NumberRegistry  # noqa: E402
from parser.cli import report  # noqa: E402
from parser.parse import parse_folder  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="konveyer", description=__doc__)
    cli.add_argument("--scheta", required=True, help="папка со счетами (PDF)")
    cli.add_argument("--payer", required=True, help="JSON с реквизитами плательщика")
    cli.add_argument("--date", help="дата платёжек, дд.мм.гггг (по умолчанию сегодня)")
    cli.add_argument("--profile", help="профиль банка")
    cli.add_argument("--out", help="куда записать файл для банка")
    cli.add_argument(
        "--include-problems",
        action="store_true",
        help="выгружать и счета с замечаниями (по умолчанию они пропускаются)",
    )
    cli.add_argument("--dry-run", action="store_true", help="не писать файл и не двигать нумерацию")
    args = cli.parse_args(argv)

    payer = Payer.from_dict(json.loads(Path(args.payer).read_text(encoding="utf-8")))
    profile = load_profile(args.profile or payer.bank_profile)
    payment_date = parse_date(args.date) if args.date else datetime.now().date()

    print("=" * 72)
    print("ЧАСТЬ 2 — распознавание")
    print("=" * 72)
    results = parse_folder(args.scheta, payer_inn=payer.inn)
    if not results:
        print("в папке нет PDF-файлов", file=sys.stderr)
        return 1
    report(results)

    chosen = results if args.include_problems else [r for r in results if r.ok]
    skipped = len(results) - len(chosen)
    if not chosen:
        print("\nнечего выгружать: все счета с замечаниями", file=sys.stderr)
        return 1

    print()
    print("=" * 72)
    print("ЧАСТЬ 1 — файл для клиент-банка")
    print("=" * 72)
    if skipped:
        print(f"пропущено счетов с замечаниями: {skipped}")

    invoices = [Invoice.from_dict(r.as_invoice_dict()) for r in chosen]
    exporter = Exporter(payer, profile, NumberRegistry(DEFAULT_REGISTRY))

    try:
        if args.dry_run:
            text, orders, problems = exporter.build(invoices, payment_date=payment_date)
            out_path = None
        else:
            name = f"1c_to_kl_{payment_date:%Y-%m-%d}_{payer.id}.txt"
            out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / name
            out_path, orders, problems = exporter.write(
                invoices, out_path, payment_date=payment_date
            )
            text = out_path.read_bytes().decode(profile.codec)
    except ExportError as exc:
        print("Выгрузка остановлена, реквизиты не прошли проверку:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    total = sum(o.invoice.total_amount for o in orders)
    print(f"платёжек: {len(orders)} на сумму {total:.2f}")
    print(f"номера:   с {orders[0].number} по {orders[-1].number}")
    if problems:
        print("\nПредупреждения генератора:")
        for problem in problems:
            print(f"  {problem}")
    if out_path:
        print(f"\nФайл для банка: {out_path}")
    else:
        print(f"\n(dry-run) строк в файле: {len(text.splitlines())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

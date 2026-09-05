"""Командный интерфейс части 2: папка со счетами -> JSON для части 1."""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from .parse import ParsedInvoice, parse_folder
from .rows import engine_name

BASE = Path(__file__).resolve().parent.parent
DEFAULT_SRC = BASE / "scheta"


def report(results: list[ParsedInvoice], verbose: bool = False) -> None:
    clean = [r for r in results if r.ok]
    total = Decimal(0)
    print(f"{'счёт':>10} {'дата':>10} {'сумма':>13} {'НДС':>10} {'ст':>3}  получатель")
    for r in results:
        d = r.data
        if not d:
            print(f"{'—':>10} {'—':>10} {'—':>13} {'—':>10} {'—':>3}  {r.source}")
            continue
        if d["total_amount"]:
            total += Decimal(d["total_amount"])
        mark = " " if r.ok else "!"
        print(
            f"{d['invoice_number'][:10]:>10} {d['invoice_date']:>10} "
            f"{d['total_amount'] or '—':>13} {d['vat_amount'] or '—':>10} "
            f"{d['vat_rate'] or '—':>3}{mark} {d['recipient_name'][:40]}"
        )
    print()
    print(f"распознано без замечаний: {len(clean)} из {len(results)}, итого {total}")

    noted = [r for r in results if r.notes]
    if noted:
        print("\nОбратите внимание:")
        for r in noted:
            for note in r.notes:
                print(f"  {r.source}: {note}")

    problems = [r for r in results if not r.ok]
    if problems:
        print("\nТребуют проверки:")
        for r in problems:
            print(f"  {r.source}")
            for problem in r.problems:
                print(f"      - {problem}")
    if verbose:
        for r in results:
            print(f"\n--- {r.source} ---")
            for row in r.rows:
                print("   |", row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="parser",
        description="Распознаёт счета на оплату (PDF) и складывает их в JSON для части 1.",
    )
    parser.add_argument("--src", default=str(DEFAULT_SRC), help="папка со счетами")
    parser.add_argument("--out", help="куда сохранить JSON со счетами")
    parser.add_argument("--payer-inn", default="", help="ИНН плательщика — проверить, что счёт наш")
    parser.add_argument(
        "--only-clean",
        action="store_true",
        help="выгрузить только счета без замечаний",
    )
    parser.add_argument("--verbose", action="store_true", help="показать распознанный текст")
    args = parser.parse_args(argv)

    source = Path(args.src)
    if not source.is_dir():
        print(f"папка не найдена: {source}", file=sys.stderr)
        return 1

    print(f"движок извлечения текста: {engine_name()}")
    print(f"папка: {source}\n")

    results = parse_folder(source, payer_inn=args.payer_inn)
    if not results:
        print("в папке нет PDF-файлов", file=sys.stderr)
        return 1

    report(results, verbose=args.verbose)

    chosen = [r for r in results if r.ok] if args.only_clean else results
    if args.out:
        payload = [r.as_invoice_dict() for r in chosen if r.data]
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nЗаписано счетов: {len(payload)} -> {args.out}")

    return 0 if all(r.ok for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())

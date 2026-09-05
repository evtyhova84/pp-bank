"""Командный интерфейс: пачка счетов -> файл для клиент-банка."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .export import ExportError, Exporter
from .models import BankProfile, Invoice, Payer, parse_date
from .numbering import NumberRegistry

BASE = Path(__file__).resolve().parent.parent
PROFILES_DIR = BASE / "profili-bankov"
DEFAULT_REGISTRY = BASE / "reestr-nomerov.json"
DEFAULT_OUT_DIR = BASE / "platezhki"


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_profile(name: str) -> BankProfile:
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        raise SystemExit(
            f"профиль банка {name!r} не найден в {PROFILES_DIR}. Доступны: {available}"
        )
    return BankProfile.from_dict(load_json(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generator",
        description="Формирует файл 1c_to_kl.txt (1CClientBankExchange) для клиент-банка.",
    )
    parser.add_argument("--payer", required=True, help="JSON с реквизитами плательщика")
    parser.add_argument("--invoices", required=True, help="JSON со списком счетов (пачка)")
    parser.add_argument("--out", help="куда записать файл (по умолчанию platezhki/)")
    parser.add_argument("--date", help="дата платёжек, дд.мм.гггг (по умолчанию сегодня)")
    parser.add_argument("--profile", help="профиль банка (перекрывает указанный у плательщика)")
    parser.add_argument("--registry", help="файл журнала нумерации")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать результат, но не писать файл и не двигать нумерацию",
    )
    args = parser.parse_args(argv)

    payer = Payer.from_dict(load_json(Path(args.payer)))
    raw_invoices = load_json(Path(args.invoices))
    if isinstance(raw_invoices, dict):
        raw_invoices = raw_invoices.get("invoices", [])
    invoices = [Invoice.from_dict(item) for item in raw_invoices]

    profile = load_profile(args.profile or payer.bank_profile)
    registry = NumberRegistry(args.registry or DEFAULT_REGISTRY)
    payment_date = parse_date(args.date) if args.date else datetime.now().date()

    exporter = Exporter(payer, profile, registry)

    try:
        if args.dry_run:
            text, orders, problems = exporter.build(invoices, payment_date=payment_date)
            out_path = None
        else:
            out_name = f"1c_to_kl_{payment_date:%Y-%m-%d}_{payer.id}.txt"
            out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / out_name
            out_path, orders, problems = exporter.write(
                invoices, out_path, payment_date=payment_date
            )
            text = out_path.read_bytes().decode(profile.codec)
    except ExportError as exc:
        print("Выгрузка остановлена, реквизиты не прошли проверку:\n", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    total = sum(o.invoice.total_amount for o in orders)
    print(f"Плательщик:    {payer.name} / счёт {payer.account}")
    print(f"Профиль банка: {profile.name} (формат {profile.format_version}, {profile.codec})")
    print(f"Дата платежа:  {payment_date:%d.%m.%Y}")
    print(f"Платёжек:      {len(orders)} на сумму {total:.2f}")
    print()
    print(f"{'№ п/п':>7}  {'Сумма':>14}  Получатель")
    for order in orders:
        print(
            f"{order.number:>7}  {order.invoice.total_amount:>14.2f}  "
            f"{order.invoice.recipient_name}"
        )

    if problems:
        print("\nПредупреждения:")
        for problem in problems:
            print(f"  {problem}")

    if args.dry_run:
        print("\n--- содержимое файла (dry-run, ничего не записано) ---")
        print(text.replace("\r\n", "\n"), end="")
    else:
        print(f"\nФайл записан: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Сборка распознанного счёта в структуру, которую ест часть 1."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .fields import (
    ORG_WORD_RE,
    cut_address,
    find_name_after_label,
    NOT_A_NAME_RE,
    split_columns,
    BUYER_LABEL_RE,
    SUPPLIER_LABEL_RE,
    find_accounts,
    find_bank_name,
    find_bic,
    find_buyer_kpp,
    split_bank_city,
    find_inn_kpp,
    find_party_by_label,
    find_recipient_name,
    find_title,
    find_totals,
    foreign_document,
    header_rows,
    recipient_kind,
)
from .items import items_summary
from .norm import looks_broken, parse_date
from .rows import ExtractionError, read_cells


@dataclass
class ParsedInvoice:
    source: str
    data: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_invoice_dict(self) -> dict:
        """Формат, который принимает генератор части 1."""
        return {k: v for k, v in self.data.items() if not k.startswith("_")}


def parse_invoice(
    path: str | Path, payer_inn: str = "", ocr_dvizhok: str | None = None
) -> ParsedInvoice:
    path = Path(path)
    result = ParsedInvoice(source=path.name)

    try:
        cells = read_cells(path, ocr_dvizhok)
    except ExtractionError as exc:
        result.problems.append(str(exc))
        return result
    rows = [" ".join(t for _, t in row) for row in cells]
    result.rows = rows

    # Шапка в две колонки («Продавец | Покупатель») — реквизиты получателя берём
    # только из левой, иначе в них попадёт ИНН покупателя.
    columns = split_columns(cells)
    requisite_rows = columns[0] if columns else rows
    head = header_rows(requisite_rows)

    # Платёжка или акт, попавшие в пачку счетов, — сказать прямо, а не
    # перечислять восемь «не найдено».
    foreign = foreign_document(rows)
    if foreign:
        result.problems.append(f"это {foreign}, а не счёт на оплату")

    title = find_title(rows)
    if title:
        _, number, date_text = title
        invoice_date = parse_date(date_text)
    else:
        number, invoice_date = "", None
        if not foreign:
            result.problems.append("не найдена строка «Счёт на оплату № … от …»")

    if invoice_date is None and title:
        result.problems.append(f"не разобрана дата счёта: {title[2]!r}")

    settlement, corr = find_accounts(head)
    bic = find_bic(head)
    inn, kpp = find_inn_kpp(head)
    bank = find_bank_name(requisite_rows, bic)

    header_name = find_recipient_name(requisite_rows) or find_name_after_label(requisite_rows)
    supplier_name, supplier_inn = find_party_by_label(requisite_rows, SUPPLIER_LABEL_RE)
    if columns:
        buyer_name, buyer_inn = find_party_by_label(columns[1], BUYER_LABEL_RE)
        if not buyer_inn:
            buyer_name = _first_name_row(columns[1]) or buyer_name
            buyer_inn, _ = find_inn_kpp(columns[1])
        buyer_kpp = find_buyer_kpp(columns[1], buyer_inn)
    else:
        buyer_name, buyer_inn = find_party_by_label(rows, BUYER_LABEL_RE)
        buyer_kpp = find_buyer_kpp(rows, buyer_inn)

    # Наименование из строки «Поставщик:» — приоритетное: в шапке счёта регулярно
    # разъезжается кернинг («ОБЩ ЕСТВО С ОГРАНИЧЕННОЙ…»), в строке поставщика — нет.
    name = header_name
    if supplier_name and (not supplier_inn or not inn or supplier_inn == inn):
        name = supplier_name
    if name and looks_broken(name):
        result.problems.append(f"наименование получателя выглядит разъехавшимся: {name!r}")
    # «(Исполнитель): Москва, пр-кт …» — в наименование попал кусок метки или адреса.
    # Это ошибка, а не заметка: такое наименование ушло бы в поле «Получатель»
    # платёжки. Деньги по ИНН и счёту дойдут, но банк вправе отклонить платёж
    # за несовпадение имени, и разбираться придётся уже после отправки.
    name_unreliable = bool(name and re.search(r"[:()]", name))
    if name_unreliable:
        result.problems.append(
            f"наименование получателя распознано ненадёжно: {name!r} — "
            "впишите его со счёта или дождитесь подстановки из справочника"
        )

    total, vat, rate, no_vat_stated = find_totals(rows)

    if not settlement:
        result.problems.append("не найден расчётный счёт получателя")
    if not corr:
        result.problems.append("не найден корреспондентский счёт")
    if not bic:
        result.problems.append("не найден БИК")
    if not inn:
        result.problems.append("не найден ИНН получателя")
    if not name:
        result.problems.append("не найдено наименование получателя")
    if total is None:
        result.problems.append("не найдена сумма к оплате")
    elif total <= 0:
        result.problems.append(f"сумма к оплате разобрана как {total} — так не бывает")
    if vat is not None and total and vat >= total:
        result.problems.append(f"сумма НДС {vat} не меньше суммы платежа {total}")
    if vat is not None and rate is None:
        result.problems.append(f"НДС {vat} есть, а ставка не определилась")
    if vat is None and not no_vat_stated:
        result.problems.append("НДС не найден и «без НДС» в счёте не написано")
    if not bank:
        result.problems.append("не найдено наименование банка получателя")

    if supplier_inn and inn and supplier_inn != inn:
        result.problems.append(
            f"ИНН в шапке ({inn}) не совпадает с ИНН в строке «Поставщик» ({supplier_inn})"
        )

    if payer_inn and buyer_inn and buyer_inn != payer_inn:
        result.problems.append(
            f"счёт выставлен не нам: покупатель {buyer_name or '?'} ИНН {buyer_inn}"
        )
    if payer_inn and inn == payer_inn:
        result.problems.append("получатель совпадает с плательщиком — похоже, это наш исходящий счёт")

    # ИНН из 10 знаков — юрлицо, у него КПП есть всегда. 12 знаков — ИП или
    # физлицо, и КПП у них не бывает. Пропавший КПП у юрлица — след распознавания.
    if len(inn) == 10 and not kpp:
        result.notes.append("у получателя-юрлица не распознан КПП — проверьте по счёту")

    name = cut_address(name)

    # Кто получатель — от этого зависит платёжка (правила в PRAVILA-samozanyatye.md).
    kind = recipient_kind(name, inn, settlement, rows)
    person = kind in ("self_employed", "person")
    if person:
        # У физлица в имени организации не бывает: всё после «ИП»/«ООО» — покупатель,
        # приклеившийся к строке со скана без колонок.
        glued = ORG_WORD_RE.search(name)
        if glued and glued.start() > 0:
            result.notes.append(f"из наименования получателя отрезан хвост {name[glued.start():]!r}")
            name = name[: glued.start()].strip(" ,;")
    if person and kpp:
        # КПП у физлица не бывает — распознался чужой (обычно покупателя)
        result.notes.append(f"у получателя-физлица КПП не бывает, распознанный {kpp} отброшен")
        kpp = ""
    if person and inn and len(inn) != 12:
        result.problems.append(f"у физлица ИНН из 12 знаков, распознан {inn}")
    # Самозанятый — штатный случай, заметка на каждом таком счёте была бы шумом:
    # признак уходит в recipient_kind, генератор сам поставит поле 20.
    if kind == "person":
        result.notes.append(
            "получатель — физлицо, а признака самозанятого в счёте нет: проверьте статус "
            "(если он не плательщик НПД, НДФЛ и взносы — за плательщиком)"
        )
    elif not person and settlement.startswith(("40817", "40820")):
        # 40817/40820 — текущий счёт физлица; у ИП или организации его быть не должно.
        chey = "ИП" if kind == "ip" else "организация"
        result.notes.append(
            f"счёт получателя {settlement} — текущий счёт физлица, "
            f"а получатель выглядит как {chey} — проверьте по счёту"
        )

    result.data = {
        "invoice_number": number,
        "invoice_date": invoice_date.isoformat() if invoice_date else "",
        "recipient_name": _short_name(name),
        "recipient_full": name,
        "recipient_inn": inn,
        "recipient_kpp": kpp,
        # org | ip | self_employed | person — часть 1 по нему ставит поле 20
        "recipient_kind": kind,
        "recipient_account": settlement,
        # «ПАО СБЕРБАНК г. Архангельск» — в файле для банка это два поля
        "recipient_bank_name": split_bank_city(bank)[0],
        "recipient_bank_city": split_bank_city(bank)[1],
        "recipient_bic": bic,
        "recipient_corr_account": corr,
        "total_amount": str(total) if total is not None else "",
        "vat_rate": str(rate) if rate is not None else None,
        "vat_amount": str(vat) if vat is not None else None,
        "vat_status": "included" if vat is not None else ("none" if no_vat_stated else "unknown"),
        # За что платим — наименования позиций через «; ». Генератор уложит их
        # в назначение платежа, сколько влезет в предел длины.
        "payment_description": items_summary(rows),
    }
    result.data["_buyer_inn"] = buyer_inn
    result.data["_buyer_name"] = buyer_name
    # Покупатель — это тот, кто платит. Сервису его реквизиты нужны, чтобы
    # предложить завести плательщика прямо из пачки счетов.
    result.data["_buyer_kpp"] = buyer_kpp
    # Флаг для сервиса: справочник получателей вправе заменить такое имя
    result.data["_name_unreliable"] = name_unreliable
    return result


def _first_name_row(rows: list[str]) -> str:
    """Первая строка колонки, похожая на наименование, а не на метку или реквизит."""
    for row in rows[1:]:
        if row and not NOT_A_NAME_RE.match(row) and not row.lower().startswith("покупател"):
            return cut_address(row)
    return ""


SHORT_FORMS = [
    (re.compile(r"общество\s+с\s+ограниченной\s+ответственностью", re.IGNORECASE), "ООО"),
    (re.compile(r"акционерное\s+общество", re.IGNORECASE), "АО"),
    (re.compile(r"публичное\s+акционерное\s+общество", re.IGNORECASE), "ПАО"),
    (re.compile(r"индивидуальный\s+предприниматель", re.IGNORECASE), "ИП"),
]


# OCR читает «ООО» как «000», когда следом идут кавычки
OOO_AS_ZEROS_RE = re.compile("\\b000\\b(?=\\s*[«\"“'])")


def _short_name(name: str) -> str:
    """Краткое наименование — в платёжке длинное не нужно."""
    short = OOO_AS_ZEROS_RE.sub("ООО", name)
    for pattern, replacement in SHORT_FORMS:
        short = pattern.sub(replacement, short)
    return " ".join(short.split())


def parse_folder(folder: str | Path, payer_inn: str = "") -> list[ParsedInvoice]:
    folder = Path(folder)
    from .ocr import IMAGE_SUFFIXES

    supported = {".pdf"} | IMAGE_SUFFIXES
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in supported)
    return [parse_invoice(p, payer_inn=payer_inn) for p in files]

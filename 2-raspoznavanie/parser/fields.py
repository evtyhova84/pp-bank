"""Извлечение отдельных полей из строк счёта."""
from __future__ import annotations

import re
from decimal import Decimal

from .norm import account_key_ok, amounts_in, clean, digits, parse_amount, parse_date, strip_quotes

# --- маркеры ---

# Знак номера обязателен, только если нет слов «на оплату»: со скана «№»
# теряется целиком («Счет на оплату 172 от 28 марта 2026 г.»), а без этих
# слов «Счет 5 от …» слишком похоже на что угодно.
TITLE_RE = re.compile(
    r"(?:счет|счёт)\s*(?:(?:на\s+оплату)\s*(?:№|N|#)?|(?:№|N|#))\s*([^\s]+)\s*от\s+(.+?)(?:\s*г\.?\s*)?$",
    re.IGNORECASE,
)
BIC_RE = re.compile(r"БИК[\s:№]*(\d{9})", re.IGNORECASE)
# 20 знаков подряд либо вразрядку, как печатает СберБизнес: «40802 810 9 4971 0010144».
# Значение нормализуется через digits().
ACCOUNT_RE = re.compile(
    r"(?<!\d)(\d{5}[ \u00a0]?\d{3}[ \u00a0]?\d[ \u00a0]?\d{4}[ \u00a0]?\d{7})(?!\d)"
)
INN_RE = re.compile(r"ИНН[\s:№]*(\d{12}|\d{10})(?!\d)", re.IGNORECASE)
KPP_RE = re.compile(r"КПП[\s:№]*(\d{9})(?!\d)", re.IGNORECASE)

ORG_RE = re.compile(
    r"(ООО|ОАО|ЗАО|ПАО|АО|НАО|ИП|Общество\s+с\s+ограниченной|"
    r"Индивидуальный\s+предприниматель|Акционерное\s+общество)",
    re.IGNORECASE,
)
BANKISH_RE = re.compile(r"банк|филиал|отделение|БИК|Корр|Сч\.?\s*№", re.IGNORECASE)

# Метки сторон — с запасом на ошибки распознавания: со скана приезжают
# «Покупатеть», «Покупатить», «Поставщик.». Хвост метки не важен, начало — да.
SUPPLIER_LABEL_RE = re.compile(
    r"^(Поставщ[а-яё]{0,3}|Исполнител[а-яё]{0,3})\s*(?:\(Исполнитель\))?\s*:?\s*", re.IGNORECASE
)
BUYER_LABEL_RE = re.compile(
    r"^(Покупат[а-яё]{1,4}|Заказчик|Плательщик)\s*(?:\(Заказчик\))?\s*:?\s*", re.IGNORECASE
)

TOTAL_LABELS = ("всего к оплате", "итого к оплате", "к оплате")
SUBTOTAL_LABELS = ("итого",)
VAT_LABELS = ("в том числе ндс", "итого ндс", "сумма ндс", "в т.ч. ндс", "в т. ч. ндс")
NO_VAT_RE = re.compile(
    r"без\s+ндс|без\s+налога|ндс\s+не\s+облагается|не\s+облагается\s+ндс|"
    r"ндс\s+не\s+предусмотрен|без\s+\(?ндс\)?|"
    r"режим\s+но\s*:?\s*нпд|самозанят",  # НПД: плательщиком НДС не является
    re.IGNORECASE,
)
VAT_RATE_RE = re.compile(r"ндс[^%\d]{0,12}?(\d{1,2})\s*%", re.IGNORECASE)

# Признак самозанятого: платформа НПД ФНС печатает «Режим НО: НПД», остальные
# пишут словами. От него зависит платёжка (поле 20, КПП), не только НДС.
NPD_RE = re.compile(
    r"режим\s+но\s*:?\s*нпд|самозанят|налог\w*\s+на\s+профессиональн\w*\s+доход|"
    r"плательщик\w*\s+нпд|применя\w+\s+нпд",
    re.IGNORECASE,
)
IP_RE = re.compile(r"\bИП\b|Индивидуальн\w+\s+предпринимател", re.IGNORECASE)
# Маркер организации целым словом: ORG_RE без границ находит «ИП» внутри «АНТИПИНА».
ORG_WORD_RE = re.compile(
    r"\b(ООО|ОАО|ЗАО|ПАО|АО|НАО|ИП)\b|Общество\s+с\s+ограниченной|"
    r"Индивидуальн\w+\s+предпринимател|Акционерное\s+общество",
    re.IGNORECASE,
)
# Чужой документ в пачке счетов: платёжка (бланк 0401060) или акт.
PAYMENT_ORDER_RE = re.compile(r"0401060|платежное\s+поручение|платёжное\s+поручение", re.IGNORECASE)
ACT_TITLE_RE = re.compile(r"^\s*акт\b", re.IGNORECASE)


def find_title(rows: list[str]) -> tuple[int, str, str] | None:
    """Индекс строки заголовка, номер счёта и текст даты."""
    for index, row in enumerate(rows):
        if "оплату" not in row.lower() and not re.match(r"\s*(счет|счёт)\b", row, re.IGNORECASE):
            continue
        match = TITLE_RE.search(row)
        if match:
            number = strip_quotes(match.group(1)).lstrip("№")
            return index, number, clean(match.group(2))
    return None


def header_rows(rows: list[str]) -> list[str]:
    title = find_title(rows)
    return rows[: title[0]] if title else rows[:12]


def find_accounts(rows: list[str]) -> tuple[str, str]:
    """(расчётный счёт, корр. счёт) — из шапки, по префиксу 301."""
    settlement = corr = ""
    for row in rows:
        for account in ACCOUNT_RE.findall(row):
            account = digits(account)
            if account.startswith("301"):
                corr = corr or account
            else:
                settlement = settlement or account
    return settlement, corr


BIC_LABEL_TAIL_RE = re.compile(r"БИК\s*[:№]?\s*$", re.IGNORECASE)


# Российский БИК всегда начинается с «04» — код страны в первых двух знаках
BARE_BIC_RE = re.compile(r"^(04\d{7})(?!\d)\s*$")


def find_bic(rows: list[str]) -> str:
    for index, row in enumerate(rows):
        match = BIC_RE.search(row)
        if match:
            return match.group(1)
        # «… БИК» в конце строки, само значение — в следующей
        if BIC_LABEL_TAIL_RE.search(row) and index + 1 < len(rows):
            value = re.match(r"^(\d{9})(?!\d)", rows[index + 1])
            if value:
                return value.group(1)
    # Значение ВЫШЕ подписи: в «образце заполнения платёжного поручения»
    # клетка с БИК напечатана над строкой «Банк получателя БИК». Берём
    # одинокое девятизначное число с «04» в начале, у которого подпись
    # «БИК» стоит в соседней строке — сверху или снизу.
    for index, row in enumerate(rows):
        value = BARE_BIC_RE.match(row.strip())
        if not value:
            continue
        sosedi = rows[max(0, index - 1) : index + 2]
        if any(re.search(r"\bБИК\b", s, re.IGNORECASE) for s in sosedi):
            return value.group(1)
    # Подпись не читается вовсе («вик», «ьик» со скана) — опознаём БИК по нему
    # самому: под верным БИК сходятся ключи расчётного и корр. счетов.
    settlement, corr = find_accounts(rows)
    for kandidat in re.findall(r"(?<!\d)(04\d{7})(?!\d)", " ".join(rows)):
        if (corr and account_key_ok(corr, kandidat, corr=True)) or (
            settlement and account_key_ok(settlement, kandidat)
        ):
            return kandidat
    return ""


BARE_LABELS_RE = re.compile(r"^ИНН\b(?!\s*[:№]?\s*\d)", re.IGNORECASE)
BARE_VALUES_RE = re.compile(r"^(\d{12}|\d{10})(?:\s+(\d{9}))?(?!\d)")


def find_inn_kpp(rows: list[str]) -> tuple[str, str]:
    inn = kpp = ""
    for index, row in enumerate(rows):
        if not inn:
            match = INN_RE.search(row)
            if match:
                inn = match.group(1)
                kpp_match = KPP_RE.search(row)
                if kpp_match:
                    kpp = kpp_match.group(1)
                continue
            # «ИНН КПП Сч. №…» — подписи в одной строке, значения в следующей
            if BARE_LABELS_RE.match(row) and index + 1 < len(rows):
                values = BARE_VALUES_RE.match(rows[index + 1])
                if values:
                    inn = values.group(1)
                    kpp = kpp or (values.group(2) or "")
        elif not kpp:
            match = KPP_RE.search(row)
            if match:
                kpp = match.group(1)
    return inn, kpp


# Двухколоночная шапка: слева продавец, справа покупатель. Если её не разделить,
# ИНН покупателя уедет в реквизиты получателя платежа — деньги уйдут не туда.
SELLER_HEAD_RE = re.compile(
    r"^(Продавец|Поставщ[а-яё]{0,3}|Исполнител[а-яё]{0,3}|Получатель платежа)\b", re.IGNORECASE
)
BUYER_HEAD_RE = re.compile(r"^(Покупат[а-яё]{1,4}|Заказчик|Плательщик)\b", re.IGNORECASE)


def split_columns(
    cells: list[list[tuple[float, str]]]
) -> tuple[list[str], list[str]] | None:
    """(строки левой колонки, строки правой) — если шапка в две колонки."""
    boundary = None
    for row in cells:
        seller = [x for x, t in row if SELLER_HEAD_RE.match(t)]
        buyer = [x for x, t in row if BUYER_HEAD_RE.match(t)]
        if seller and buyer and max(buyer) > min(seller):
            boundary = min(x for x in buyer if x > min(seller))
            break
    if boundary is None:
        return None

    left, right = [], []
    for row in cells:
        left_text = clean(" ".join(t for x, t in row if x < boundary))
        right_text = clean(" ".join(t for x, t in row if x >= boundary))
        if left_text:
            left.append(left_text)
        if right_text:
            right.append(right_text)
    return left, right


# Строка, которая названием быть не может: подпись реквизита или вторая
# строка метки в скобках — «(Исполнитель): Москва, пр-кт …» — это адрес.
NOT_A_NAME_RE = re.compile(
    r"^(ИНН|КПП|БИК|Корр|Расчетный|Расчётный|Сч|Счет|Счёт|Режим|Адрес|тел|Банк)\b|^\(",
    re.IGNORECASE,
)


def find_name_after_label(rows: list[str]) -> str:
    """Наименование строкой ниже метки «Продавец» — для счетов без ООО/ИП в названии.

    Так выглядят счета самозанятых с платформы НПД ФНС: там просто ФИО.
    """
    for index, row in enumerate(rows):
        if not SELLER_HEAD_RE.match(row):
            continue
        for candidate in rows[index + 1 : index + 4]:
            if candidate and not NOT_A_NAME_RE.match(candidate) and not BANKISH_RE.search(candidate):
                return cut_address(candidate)
    return ""


def _is_name_row(row: str) -> bool:
    return bool(ORG_RE.search(row)) and not BANKISH_RE.search(row)


def find_recipient_name(rows: list[str]) -> str:
    """Наименование получателя из шапки — ближайшее к метке «Получатель»."""
    head = header_rows(rows)
    marker = next(
        (i for i, row in enumerate(head) if re.match(r"^Получатель\b", row, re.IGNORECASE)),
        None,
    )
    candidates = [(i, row) for i, row in enumerate(head) if _is_name_row(row)]
    if not candidates:
        return ""
    if marker is None:
        return strip_quotes(candidates[-1][1])
    index, row = min(candidates, key=lambda item: abs(item[0] - marker))
    # «ООО "БК АРЕНДА" Сч. №40702810129370003742» — отрезаем хвост со счётом.
    # Со скана к названию прилипают и соседние клетки: «… КПП 772101001 сч. 4070…»
    row = re.split(
        r"\s*(?:\bСч\.\s*№?|\bСчет\s*№|\bИНН\b|\bКПП\b|\bБИК\b)", row, flags=re.IGNORECASE
    )[0]
    return strip_quotes(row)


def find_party_by_label(rows: list[str], label_re: re.Pattern) -> tuple[str, str]:
    """(наименование, ИНН) из строки вида «Поставщик: ООО "X", ИНН 123..»."""
    for index, row in enumerate(rows):
        if not label_re.match(row):
            continue
        text = label_re.sub("", row)
        # продолжение может быть на следующей строке
        joined = text
        # «…АРСЕНТЬЕВА ЕКАТЕРИНА АЛЕКСЕЕВНА, ИНН» — подпись есть, значение перенеслось
        if index + 1 < len(rows) and not INN_RE.search(text):
            joined = f"{text} {rows[index + 1]}"
        inn_match = INN_RE.search(joined)
        name = re.split(r",?\s*ИНН\b", joined)[0]
        if not inn_match:
            # «Поставщик (получатель платежа)» — одна метка, реквизитов в строке
            # нет. Но если после метки стоит название организации, а ИНН
            # со скана прочитался с ошибкой — название всё равно берём.
            if "ИНН" in joined and _is_name_row(name):
                return cut_address(name), ""
            return "", ""
        return cut_address(name), inn_match.group(1)
    return "", ""


def find_buyer_kpp(rows: list[str], buyer_inn: str) -> str:
    """КПП покупателя — тот, что стоит рядом с его ИНН.

    Ищем только около ИНН покупателя: в счёте есть ещё КПП поставщика, и взять
    первый попавшийся значит перепутать стороны. У ИП КПП нет — вернётся пусто.
    """
    if not buyer_inn:
        return ""
    for index, row in enumerate(rows):
        if buyer_inn not in row:
            continue
        # значение может уехать на следующую строку: «ИНН 1661034770,» / «КПП 166101001»
        okrestnost = " ".join(rows[index : index + 2])
        tail = okrestnost[okrestnost.find(buyer_inn) + len(buyer_inn) :]
        match = KPP_RE.search(tail)
        if match:
            return match.group(1)
        # «ИНН КПП» подписями, значения строкой ниже: «1661034770 166101001»
        values = re.search(re.escape(buyer_inn) + r"\s+(\d{9})(?!\d)", okrestnost)
        if values:
            return values.group(1)
    return ""


ADDRESS_TAIL_RE = re.compile(
    r"(?:,\s*)?\b\d{6}\b"  # почтовый индекс
    r"|,\s*(?:ул|улица|г\.|гор\.|город|д\.|дом|кв\.|пом|помещ|оф\.|офис|пр-т|проспект|"
    r"Респ|Республика|обл|область|р-н|тел|Адрес|адрес)\b",
    re.IGNORECASE,
)


# «ИП Сенотрусова Валерия Вячеславовна 191 332,00 ₽» — сумма счёта в строке с именем
AMOUNT_TAIL_RE = re.compile(r"\s+\d[\d\s\u00a0]*[.,]\d{2}\s*(?:₽|руб\.?)?\s*$")


def cut_address(name: str) -> str:
    """Отрезает адрес, телефон и сумму, приклеившиеся к наименованию."""
    name = AMOUNT_TAIL_RE.sub("", name)
    return strip_quotes(ADDRESS_TAIL_RE.split(name, maxsplit=1)[0])


def recipient_kind(name: str, inn: str, account: str, rows: list[str]) -> str:
    """Кто получатель: org | ip | self_employed | person (пусто — не понять).

    От этого зависит платёжка: физлицу (самозанятому в том числе) в поле 20
    ставится код вида дохода, КПП у него не бывает, ИНН — 12 знаков.
    ИП с признаком НПД остаётся ИП: платят на его расчётный счёт как бизнесу.
    """
    # Маркер ИП — только в начале: со скана без колонок в строку с ФИО продавца
    # приклеивается покупатель («СЕРГЕЕВА Н. Н. ИП Немцева А. А.»).
    if IP_RE.match(name):
        return "ip"
    if NPD_RE.search(" ".join(rows)):
        return "self_employed"
    if len(inn) == 10 or ORG_WORD_RE.search(name):
        return "org"
    if account.startswith(("40817", "40820")) or len(inn) == 12:
        return "person"
    return ""


def foreign_document(rows: list[str]) -> str:
    """Что за документ, если это не счёт: «платёжное поручение», «акт» или пусто."""
    head = " ".join(rows[:8])
    if PAYMENT_ORDER_RE.search(head):
        return "платёжное поручение"
    if rows and ACT_TITLE_RE.match(rows[0]):
        return "акт"
    return ""


# Подпись клетки «Банк получателя», в том числе разорванная сканом:
# «Банк», «Банк п теля». Названием банка не является.
BANK_LABEL_RE = re.compile(r"^\s*Банк(\s+[а-яё]{1,12}){0,2}\s*$", re.IGNORECASE)
# Город в конце названия банка: «… ПАО СБЕРБАНК г. Архангельск»
CITY_TAIL_RE = re.compile(r"\s*(\bг\.?\s*[А-ЯЁ][А-Яа-яё\-\s.]*)$")
# Название кончилось на «г.» — сам город переехал на следующую строку
DANGLING_CITY_RE = re.compile(r"\bг\.?\s*$")
SINGLE_CITY_RE = re.compile(r"^\s*(г\.?\s*)?[А-ЯЁ][А-Яа-яё\-]+(\s+[А-ЯЁ][А-Яа-яё\-]+)?\s*$")


def _bez_rekvizitov(row: str) -> str:
    """Строка шапки без БИК, счетов и их подписей — остаётся название банка."""
    if re.match(r"^Банк\s+получателя", row, re.IGNORECASE):
        row = re.sub(r"^Банк\s+получателя\s*", "", row, flags=re.IGNORECASE)
    row = BIC_RE.sub("", row)
    # Одинокая подпись «БИК» — значение уехало в другую строку,
    # а подпись без него в названии банка не нужна
    row = re.sub(r"\bБИК\b\s*[:№]?", "", row, flags=re.IGNORECASE)
    row = ACCOUNT_RE.sub("", row)
    return re.sub(r"(Корр\.?\s*)?Сч(ет|ёт)?\.?\s*№?", "", row, flags=re.IGNORECASE)


def find_bank_name(rows: list[str], bic: str = "") -> str:
    head = header_rows(rows)
    parts = []
    for index, row in enumerate(head):
        if not re.search(r"банк|филиал|отделение", row, re.IGNORECASE):
            continue
        row = _bez_rekvizitov(row)
        if BANK_LABEL_RE.match(row):
            continue  # «Банк получателя», «Банк п теля» — подпись клетки, не название
        obryvaetsya_na_g = bool(DANGLING_CITY_RE.search(row.strip()))
        row = strip_quotes(row)
        if not row or re.fullmatch(r"[\W_]+", row):
            continue
        # «… ПАО СБЕРБАНК г.» и на следующей строке «Архангельск»: клетка
        # узкая, город перенёсся. Забираем его, иначе он потеряется.
        if obryvaetsya_na_g and index + 1 < len(head):
            sleduyushchaya = head[index + 1].strip()
            if SINGLE_CITY_RE.match(sleduyushchaya) and not BANK_LABEL_RE.match(sleduyushchaya):
                gorod = re.sub(r"^г\.?\s*", "", sleduyushchaya)
                row = f"{DANGLING_CITY_RE.sub('', row).rstrip()} г. {gorod}"
        parts.append(row)
    if not parts and bic:
        # Слово «банк» не прочиталось («ПАО ЪАНК САНКТЛЕТЕРБУРГ»), но БИК опознан
        # по ключам счетов — название банка стоит в той же клетке, перед БИК.
        for row in head:
            if bic not in re.sub(r"\s", "", row):
                continue
            kletka = re.split(rf"\S{{0,4}}\s*{bic}", re.sub(r"(?<=\d)\s+(?=\d)", "", row))[0]
            kletka = strip_quotes(_bez_rekvizitov(kletka))
            if len(re.findall(r"[А-Яа-яЁёA-Za-z]", kletka)) >= 3:
                parts.append(kletka)
                break
    return strip_quotes(" ".join(parts))


def split_bank_city(bank: str) -> tuple[str, str]:
    """«ПАО СБЕРБАНК г. Архангельск» -> («ПАО СБЕРБАНК», «г. Архангельск»).

    В файле для банка это разные поля: ПолучательБанк1 и ПолучательБанк2.
    Написание города сохраняем как напечатано.
    """
    match = CITY_TAIL_RE.search(bank)
    if not match or match.start() == 0:
        return bank.strip(), ""
    gorod = " ".join(match.group(1).split()).rstrip(" .,")
    if len(gorod) < 4:
        return bank.strip(), ""
    return bank[: match.start()].strip(" ,"), gorod


RATE_IN_LABEL_RE = re.compile(r"\d{1,2}(?:[.,]\d+)?\s*%")
SPELLED_OUT_RE = re.compile(r"рубл|копе", re.IGNORECASE)


def _labelled_amount(row: str, label: str) -> Decimal | None:
    """Число сразу после метки. Ставка «22%» и сумма прописью не в счёт."""
    lower = row.lower()
    position = lower.find(label)
    if position < 0:
        return None
    tail = row[position + len(label) :]
    if SPELLED_OUT_RE.search(tail):
        return None  # «Итого к оплате: Двадцать две тысячи ... 00 копеек»
    tail = RATE_IN_LABEL_RE.sub(" ", tail)
    values = amounts_in(tail)
    return values[0] if values else None


VALID_RATES = (Decimal(5), Decimal(7), Decimal(10), Decimal(18), Decimal(20), Decimal(22))


def _choose_total(
    candidates: list[Decimal | None], vat: Decimal | None, rate: Decimal | None
) -> Decimal | None:
    """Итог печатается в счёте трижды: «Итого», «Всего к оплате», «на сумму».

    На сканах какой-то из них распознаётся криво — например «12» вместо «12 580,00».
    Кандидаты сверяются между собой и с НДС: сумма меньше НДС невозможна,
    а верная сумма даёт заявленную ставку.
    """
    values = [c for c in candidates if c is not None]
    if not values:
        return None

    if vat is not None:
        plausible = [c for c in values if c > vat]
        if plausible:
            values = plausible

    if vat is not None and rate is not None:
        matching = [
            c
            for c in values
            if c > vat and (vat * 100 / (c - vat)).quantize(Decimal("1")) == rate
        ]
        if matching:
            values = matching

    # чаще всего повторившееся значение; при равенстве — первое по порядку меток
    return max(values, key=lambda c: (values.count(c), -values.index(c)))


def find_totals(rows: list[str]):
    """(всего к оплате, сумма НДС, ставка НДС, «без НДС» написано явно)."""
    total = subtotal = vat = None
    rate: Decimal | None = None
    saw_no_vat = False

    for row in rows:
        lower = row.lower()

        for label in TOTAL_LABELS:
            if label in lower:
                value = _labelled_amount(row, label)
                if value is not None:
                    total = total or value

        if any(label in lower for label in VAT_LABELS):
            for label in VAT_LABELS:
                if label in lower:
                    value = _labelled_amount(row, label)
                    if value is not None and vat is None:
                        vat = value
                    match = VAT_RATE_RE.search(row)
                    if match and rate is None:
                        rate = Decimal(match.group(1))
                    break
        elif NO_VAT_RE.search(row):
            saw_no_vat = True

        if subtotal is None:
            for label in SUBTOTAL_LABELS:
                if lower.startswith(label) or f" {label}:" in lower:
                    value = _labelled_amount(row, label)
                    if value is not None:
                        subtotal = value

    summary = None
    for row in rows:
        if "на сумму" in row.lower():
            summary = _labelled_amount(row, "на сумму")
            if summary is not None:
                break

    total = _choose_total([total, subtotal, summary], vat, rate)

    if vat is not None and vat == 0:
        # «Сумма НДС: 0,00» — это не «не нашли», это честный ноль
        vat = None
        saw_no_vat = True
    if vat is None and saw_no_vat:
        rate = None

    # ставка не написана словом — выводим из сумм: НДС в том числе
    if vat is not None and rate is None and total and total > vat:
        computed = (vat * 100 / (total - vat)).quantize(Decimal("1"))
        if computed in (Decimal(5), Decimal(7), Decimal(10), Decimal(18), Decimal(20), Decimal(22)):
            rate = computed

    return total, vat, rate, saw_no_vat

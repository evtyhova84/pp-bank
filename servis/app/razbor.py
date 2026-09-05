"""Распознавание одного документа для сервиса: две ступени и проверки.

    файл -> текстовый слой PDF или локальный OCR -> контрольные суммы
         -> сошлось: в пачку
         -> не сошлось: второй движок, склейка полей из обоих -> те же проверки
         -> не сошлось и включена вторая ступень: vision-модель -> те же проверки
         -> и там не сошлось: человеку, с указанием конкретных полей

Проверки одни и те же на всех путях, и это главное. Реквизиты счёта
самопроверяемы — ИНН, БИК, расчётный и корр. счёт имеют контрольные суммы,
ставка НДС сходится с суммами. Ошибка в одной цифре почти наверняка их ломает,
поэтому кривой разбор уходит человеку, а не в банк.

Движки ошибаются по-разному: один теряет клетку с расчётным счётом, другой —
знак «№» в заголовке. Брать лучший разбор целиком значит терять то, что второй
прочитал верно. Поэтому поля склеиваются: за основу берётся разбор с меньшим
числом замечаний, а недостающее или не прошедшее контрольную сумму добирается
из другого — только если там оно контрольную сумму проходит.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .yadro import Invoice, parse_date, parse_invoice

# Вторая ступень включается настройкой: по умолчанию наружу ничего не уходит
VTORAYA_STUPEN = os.environ.get("PP_VISION", "").lower() in ("1", "on", "yes", "да")


@dataclass
class Razobrano:
    """Результат разбора одного файла."""

    fayl: str
    data: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dvizhok: str = ""
    # Замечания самого парсера — отдельно от контрольных сумм. Контрольные
    # суммы после подстановки из справочника пересчитываются заново, а эти
    # замечания («счёт выставлен не нам», «не найден БИК») должны пережить
    # пересчёт: однажды они потерялись, и чужой счёт ушёл в файл как чистый.
    iz_parsera: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.data:
            return "failed"  # прочитать не удалось вовсе
        return "ok" if not self.problems else "problem"


def proverit_summy(data: dict) -> list[str]:
    """Контрольные суммы реквизитов — те же, что перед записью файла в банк.

    Гоняем их сразу после распознавания, а не только при выгрузке: сотрудник
    должен увидеть проблему на экране проверки, а не в момент, когда файл
    уже собран и не пишется.
    """
    from generator.validate import validate_invoice

    try:
        invoice = Invoice.from_dict({k: v for k, v in data.items() if not k.startswith("_")})
    except (ValueError, TypeError) as exc:
        return [f"данные счёта не складываются в платёжку: {exc}"]

    return [str(p) for p in validate_invoice(invoice) if p.level == "error"]


def _dvizhok_fayla(path: Path) -> str:
    from parser import ocr, rows

    if ocr.is_image(path):
        try:
            return ocr.vybrat_dvizhok()
        except ocr.OcrUnavailable:
            return "нет движка OCR"
    return rows.engine_name()


def _dvizhki_dlya(path: Path) -> list[str | None]:
    """Чем пробовать читать документ, в порядке предпочтения.

    У PDF вариант один — текстовый слой. У картинки движков может быть
    несколько, и они ошибаются по-разному: один спотыкается на рамках таблицы,
    другой на тенях от сгиба. Раз результат проверяем контрольными суммами,
    можно просто попробовать вторым тот счёт, который не сошёлся у первого.
    """
    from parser import ocr

    if not ocr.is_image(path):
        return [None]
    return list(ocr.dostupnye_dvizhki()) or [None]


# --- склейка полей из нескольких разборов ---


def _s(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()


def _bank_shoditsya(data: dict) -> bool:
    """БИК подтверждён хотя бы одним счётом: ключ счёта считается вместе с БИК."""
    from generator.validate import check_account, check_bic

    bic = _s(data, "recipient_bic")
    return check_bic(bic) and (
        check_account(_s(data, "recipient_account"), bic)
        or check_account(_s(data, "recipient_corr_account"), bic, corr=True)
    )


def _imya_godnoe(data: dict) -> bool:
    from parser.norm import looks_broken

    name = _s(data, "recipient_full") or _s(data, "recipient_name")
    return bool(name) and not data.get("_name_unreliable") and not looks_broken(name)


def _data_godnaya(text: str) -> bool:
    try:
        parse_date(text)
    except ValueError:
        return False
    return True


def slit(osnova: dict, drugoy: dict) -> tuple[dict, list[str]]:
    """Добрать в `osnova` то, что у `drugoy` прочиталось лучше.

    Правило одно на все поля: берём чужое значение, только если своё пусто
    или не проходит проверку, а чужое проходит. Реквизиты, по которым уходят
    деньги, проверяются контрольными суммами; для остального «проверка» —
    что поле вообще есть. Возвращает (данные, что взято).
    """
    from generator.validate import check_account, check_inn

    data = dict(osnova)
    vzyato: list[str] = []

    if not check_inn(_s(data, "recipient_inn")) and check_inn(_s(drugoy, "recipient_inn")):
        data["recipient_inn"] = drugoy["recipient_inn"]
        vzyato.append("ИНН")

    if not _bank_shoditsya(data) and _bank_shoditsya(drugoy):
        for pole in ("recipient_bic", "recipient_account", "recipient_corr_account"):
            data[pole] = drugoy.get(pole) or ""
        vzyato.append("БИК и счета")
    bic = _s(data, "recipient_bic")
    for pole, korr, imya in (
        ("recipient_account", False, "расчётный счёт"),
        ("recipient_corr_account", True, "корр. счёт"),
    ):
        if not check_account(_s(data, pole), bic, corr=korr) and check_account(
            _s(drugoy, pole), bic, corr=korr
        ):
            data[pole] = drugoy[pole]
            vzyato.append(imya)

    if not re.fullmatch(r"\d{9}", _s(data, "recipient_kpp")) and re.fullmatch(
        r"\d{9}", _s(drugoy, "recipient_kpp")
    ):
        data["recipient_kpp"] = drugoy["recipient_kpp"]
        vzyato.append("КПП")

    if not _s(data, "invoice_number") and _s(drugoy, "invoice_number"):
        data["invoice_number"] = drugoy["invoice_number"]
        vzyato.append("номер счёта")
    if not _data_godnaya(_s(data, "invoice_date")) and _data_godnaya(_s(drugoy, "invoice_date")):
        data["invoice_date"] = drugoy["invoice_date"]
        vzyato.append("дата счёта")

    if not _s(data, "total_amount") and _s(drugoy, "total_amount"):
        for pole in ("total_amount", "vat_amount", "vat_rate", "vat_status"):
            data[pole] = drugoy.get(pole)
        vzyato.append("сумма")
    elif (
        data.get("vat_status") == "unknown"
        and drugoy.get("vat_status") in ("included", "none")
        and _s(drugoy, "total_amount") == _s(data, "total_amount")
    ):
        for pole in ("vat_amount", "vat_rate", "vat_status"):
            data[pole] = drugoy.get(pole)
        vzyato.append("НДС")

    if not _imya_godnoe(data) and _imya_godnoe(drugoy):
        data["recipient_name"] = drugoy["recipient_name"]
        data["recipient_full"] = drugoy["recipient_full"]
        data["_name_unreliable"] = False
        vzyato.append("наименование")

    if not _s(data, "recipient_bank_name") and _s(drugoy, "recipient_bank_name"):
        data["recipient_bank_name"] = drugoy["recipient_bank_name"]
        data["recipient_bank_city"] = drugoy.get("recipient_bank_city") or ""
        vzyato.append("банк")
    elif not _s(data, "recipient_bank_city") and _s(drugoy, "recipient_bank_city"):
        data["recipient_bank_city"] = drugoy["recipient_bank_city"]

    if not _s(data, "payment_description") and _s(drugoy, "payment_description"):
        data["payment_description"] = drugoy["payment_description"]

    if not check_inn(_s(data, "_buyer_inn")) and check_inn(_s(drugoy, "_buyer_inn")):
        for pole in ("_buyer_inn", "_buyer_name", "_buyer_kpp"):
            data[pole] = drugoy.get(pole) or ""
        vzyato.append("покупатель")

    return data, vzyato


# Замечание парсера -> проверка, что оно всё ещё в силе после склейки.
# Парсер формулирует замечания по данным; когда поле добрано из другого
# движка, соответствующее замечание снимается — а контрольные суммы затем
# пересчитываются заново, так что кривое значение всё равно не пройдёт.
ZAKRYVAETSYA = (
    ("не найден расчётный счёт", lambda d: bool(_s(d, "recipient_account"))),
    ("не найден корреспондентский счёт", lambda d: bool(_s(d, "recipient_corr_account"))),
    ("не найден БИК", lambda d: bool(_s(d, "recipient_bic"))),
    ("не найден ИНН получателя", lambda d: bool(_s(d, "recipient_inn"))),
    ("не найдено наименование получателя", _imya_godnoe),
    ("наименование получателя распознано ненадёжно", _imya_godnoe),
    ("наименование получателя выглядит разъехавшимся", _imya_godnoe),
    ("не найдена сумма к оплате", lambda d: bool(_s(d, "total_amount"))),
    ("НДС не найден", lambda d: d.get("vat_status") in ("included", "none")),
    ("не найдено наименование банка", lambda d: bool(_s(d, "recipient_bank_name"))),
    (
        "не найдена строка «Счёт на оплату",
        lambda d: bool(_s(d, "invoice_number")) and _data_godnaya(_s(d, "invoice_date")),
    ),
    ("не разобрана дата счёта", lambda d: _data_godnaya(_s(d, "invoice_date"))),
)


def _peresobrat_zamechaniya(iz_parsera: list[str], data: dict, payer_inn: str) -> list[str]:
    """Замечания парсера после склейки: закрытые снимаем, про покупателя пересчитываем."""
    itog = []
    for p in iz_parsera:
        if p.startswith("счёт выставлен не нам"):
            continue  # ниже — по склеенному покупателю
        if any(p.startswith(nachalo) and v_sile(data) for nachalo, v_sile in ZAKRYVAETSYA):
            continue
        itog.append(p)
    buyer_inn = _s(data, "_buyer_inn")
    if payer_inn and buyer_inn and buyer_inn != payer_inn:
        itog.append(
            f"счёт выставлен не нам: покупатель {_s(data, '_buyer_name') or '?'} ИНН {buyer_inn}"
        )
    return itog


def _luchshiy(kandidaty: list[Razobrano]) -> Razobrano:
    s_dannymi = [k for k in kandidaty if k.data]
    if not s_dannymi:
        return kandidaty[0]
    return min(s_dannymi, key=lambda k: len(k.problems))


def skleit(kandidaty: list[Razobrano], payer_inn: str = "") -> Razobrano:
    """Лучший разбор, дополненный из остальных, с заново посчитанными замечаниями."""
    osnova = _luchshiy(kandidaty)
    if not osnova.data or not osnova.problems:
        return osnova

    data = dict(osnova.data)
    dvizhki = [osnova.dvizhok]
    zapisi: list[str] = []
    for drugoy in kandidaty:
        if drugoy is osnova or not drugoy.data:
            continue
        data, vzyato = slit(data, drugoy.data)
        if vzyato:
            dvizhki.append(drugoy.dvizhok)
            zapisi.append(f"из движка {drugoy.dvizhok} взято: {', '.join(vzyato)}")
    if not zapisi:
        return osnova

    iz_parsera = _peresobrat_zamechaniya(osnova.iz_parsera, data, payer_inn)
    notes = [
        n
        for n in osnova.notes
        if not (n.startswith("у получателя-юрлица не распознан КПП") and _s(data, "recipient_kpp"))
    ]
    return Razobrano(
        fayl=osnova.fayl,
        data=data,
        problems=iz_parsera + proverit_summy(data),
        notes=notes + zapisi,
        dvizhok="+".join(dvizhki),
        iz_parsera=iz_parsera,
    )


# --- вход ---


def razobrat(
    path: str | Path, payer_inn: str = "", vtoraya_stupen: bool | None = None
) -> Razobrano:
    """Разобрать один счёт. Ошибку не поднимаем: она — часть результата."""
    path = Path(path)
    dvizhki = _dvizhki_dlya(path)
    kandidaty: list[Razobrano] = []

    for dvizhok in dvizhki:
        pervaya = parse_invoice(path, payer_inn=payer_inn, ocr_dvizhok=dvizhok)
        kandidat = Razobrano(
            fayl=path.name,
            data=dict(pervaya.data),
            problems=list(pervaya.problems),
            notes=list(pervaya.notes),
            dvizhok=dvizhok or _dvizhok_fayla(path),
            iz_parsera=list(pervaya.problems),
        )
        if kandidat.data:
            kandidat.problems += proverit_summy(kandidat.data)
        kandidaty.append(kandidat)
        if kandidat.data and not kandidat.problems:
            break  # сошлось — дальше пробовать незачем

    itog = skleit(kandidaty, payer_inn)

    if itog.data and itog.dvizhok and len(dvizhki) > 1:
        itog.notes.append(f"распознано движком {itog.dvizhok}")

    escalate = VTORAYA_STUPEN if vtoraya_stupen is None else vtoraya_stupen
    if not itog.problems or not escalate:
        return itog

    # Первая ступень не справилась — пробуем модель
    from parser import vision

    if not vision.dostupno():
        itog.notes.append("вторая ступень распознавания не настроена")
        return itog

    try:
        data = vision.raspoznat(path)
    except (vision.VisionFailed, vision.VisionUnavailable) as exc:
        itog.notes.append(f"вторая ступень не помогла: {exc}")
        return itog

    zamechaniya = proverit_summy(data)
    if payer_inn and data.get("_buyer_inn") and data["_buyer_inn"] != payer_inn:
        zamechaniya.append(
            f"счёт выставлен не нам: покупатель {data.get('_buyer_name') or '?'} "
            f"ИНН {data['_buyer_inn']}"
        )

    if zamechaniya:
        # Обе ступени не сошлись — человеку, и показываем замечания той,
        # что была ближе к успеху.
        itog.notes.append("вторая ступень тоже не сошлась по контрольным суммам")
        if len(zamechaniya) < len(itog.problems):
            itog.data, itog.problems, itog.dvizhok = data, zamechaniya, "claude-vision"
        return itog

    itog.data = data
    itog.problems = []
    itog.iz_parsera = []
    itog.dvizhok = "claude-vision"
    itog.notes.append("разобрано второй ступенью — сверьте реквизиты со счётом")
    return itog

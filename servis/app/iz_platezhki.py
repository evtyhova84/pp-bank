"""Реквизиты плательщика из уже существующей платёжки.

Набивать двадцатизначный счёт и корр. счёт руками — ровно тот случай, где
опечатка тихо проходит глазами и всплывает в банке. А реквизиты уже напечатаны
в любой платёжке, которую организация делала раньше. Поэтому их проще прочитать.

Понимаем два вида источника:

  1. Файл обмена с клиент-банком, `1CClientBankExchange` — выгрузка из 1С
     (`1c_to_kl.txt`) или выписка из банка (`kl_to_1c.txt`). Это точный источник:
     реквизиты лежат готовыми полями, гадать не надо.
  2. Платёжное поручение в PDF или снимком — разбираем так же, как счета.

Что бы ни пришло, результат проверяется контрольными суммами и показывается
человеку в форме до сохранения. Молча в справочник ничего не попадает.

В платёжке две стороны, и наша организация может стоять в любой из них:
в своей исходящей платёжке она плательщик, а в платёжке, которой заплатили
нам (её выдаёт наш банк как подтверждение поступления), — получатель.
Поэтому читаются обе стороны, а какая наша — решается по признакам: в файле
обмена это счёт из шапки, в PDF — банк, выдавший документ (его БИК стоит
в отметках внизу и совпадает с банком одной из сторон). Когда признаков нет,
берётся плательщик, а вторая сторона предлагается человеку одной кнопкой.
"""
from __future__ import annotations

import re
from pathlib import Path

MARKER = "1CClientBankExchange"

# Поля файла обмена: наши имена -> имена в файле, для плательщика и получателя
POLYA_1C = {
    "account": ("ПлательщикСчет", "ПолучательСчет"),
    "full_name": ("Плательщик1", "Получатель1"),
    "inn": ("ПлательщикИНН", "ПолучательИНН"),
    "kpp": ("ПлательщикКПП", "ПолучательКПП"),
    "bank_name": ("ПлательщикБанк1", "ПолучательБанк1"),
    "bank_city": ("ПлательщикБанк2", "ПолучательБанк2"),
    "bic": ("ПлательщикБИК", "ПолучательБИК"),
    "corr_account": ("ПлательщикКорсчет", "ПолучательКорсчет"),
}

PUSTO = {
    "code": "",
    "name": "",
    "full_name": "",
    "inn": "",
    "kpp": "",
    "account": "",
    "bank_name": "",
    "bank_city": "",
    "bic": "",
    "corr_account": "",
}


class NeRazobrano(RuntimeError):
    """Не разобрали. Строки документа несём с собой: по ним видно, почему."""

    def __init__(self, soobshchenie: str, stroki: list[str] | None = None) -> None:
        super().__init__(soobshchenie)
        self.stroki = list(stroki or [])


# --- файл обмена с клиент-банком ---


def pohozhe_na_obmen(dannye: bytes) -> bool:
    return MARKER.encode("cp1251") in dannye[:400] or MARKER.encode("utf-8") in dannye[:400]


def _stroki_obmena(dannye: bytes) -> list[str]:
    for kodirovka in ("cp1251", "utf-8-sig", "utf-8"):
        try:
            text = dannye.decode(kodirovka)
        except UnicodeDecodeError:
            continue
        if MARKER in text:
            return text.splitlines()
    raise NeRazobrano("файл обмена не прочитался — неизвестная кодировка")


def iz_obmena(dannye: bytes) -> dict:
    """Реквизиты своей организации из файла обмена с клиент-банком."""
    stroki = _stroki_obmena(dannye)

    shapka: dict[str, str] = {}
    dokumenty: list[dict[str, str]] = []
    tekushchiy: dict[str, str] | None = None
    for stroka in stroki:
        if "=" not in stroka:
            if stroka.strip() == "КонецДокумента" and tekushchiy is not None:
                dokumenty.append(tekushchiy)
                tekushchiy = None
            continue
        kluch, _, znachenie = stroka.partition("=")
        kluch, znachenie = kluch.strip(), znachenie.strip()
        if kluch == "СекцияДокумент":
            if tekushchiy is not None:
                dokumenty.append(tekushchiy)
            tekushchiy = {}
            continue
        (tekushchiy if tekushchiy is not None else shapka)[kluch] = znachenie
    if tekushchiy:
        dokumenty.append(tekushchiy)

    if not dokumenty:
        raise NeRazobrano("в файле обмена нет ни одного платёжного документа")

    # «Наш» счёт объявлен в шапке файла: РасчСчет. По нему и понимаем, с какой
    # стороны в документе стоит наша организация — в выписке она бывает и там,
    # и там.
    nash_schet = shapka.get("РасчСчет", "").strip()

    def storona_dokumenta(dokument: dict, storona: int) -> dict | None:
        itog = {nashe: dokument.get(imena[storona], "").strip() for nashe, imena in POLYA_1C.items()}
        if not (itog["inn"] and itog["account"] and itog["bic"]):
            return None
        itog["name"] = _kratkoe(itog["full_name"])
        return {**PUSTO, **itog, "_storona": STORONY[storona]}

    for dokument in dokumenty:
        for storona in (0, 1):
            schet = dokument.get(POLYA_1C["account"][storona], "").strip()
            if not schet or (nash_schet and schet != nash_schet):
                continue
            itog = storona_dokumenta(dokument, storona)
            if itog is None:
                continue
            drugaya = storona_dokumenta(dokument, 1 - storona)
            itog["_drugaya_storona"] = drugaya
            itog["_pochemu"] = (
                f"этот счёт объявлен своим в шапке файла ({nash_schet})" if nash_schet else ""
            )
            return itog

    raise NeRazobrano(
        "в файле обмена не нашлись реквизиты организации"
        + (f" по счёту {nash_schet}" if nash_schet else "")
    )


STORONY = ("плательщик", "получатель")


# --- платёжное поручение документом ---

# Двенадцать цифр проверяем первыми и запрещаем цифру следом: у ИП ИНН из
# двенадцати знаков, и вариант на десять откусывал бы от него первые десять.
# Такой огрызок не сходится по контрольной сумме, но лучше и не создавать его.
# Подпись отделяем не границей слова, а границей БУКВ: «\b» между «ИНН»
# и «1661034770» не стоит — буква и цифра для регулярного выражения одно
# и то же, — а в бланках подпись регулярно приклеена к числу без пробела.
BUKVA = "А-Яа-яЁёA-Za-z"
INN_RE = re.compile(rf"(?<![{BUKVA}])ИНН(?![{BUKVA}])[:\s]*(\d{{12}}|\d{{10}})(?!\d)")
KPP_RE = re.compile(rf"(?<![{BUKVA}])КПП(?![{BUKVA}])[:\s]*(\d{{9}})")
BIC_RE = re.compile(rf"(?<![{BUKVA}])БИК(?![{BUKVA}])[:\s]*(\d{{9}})")
SCHET_RE = re.compile(r"\b(\d{20})\b")
# Где заканчивается половина плательщика и начинается половина получателя
GRANITSA_RE = re.compile(r"Банк\s+получателя|^\s*Получатель\b", re.IGNORECASE)
# С этой подписи начинается половина получателя (подписи в бланке стоят под клетками)
NACHALO_POLUCHATELYA_RE = re.compile(r"Банк\s+плательщика", re.IGNORECASE)
# Подвал: назначение подписано, дальше — отметки банка, штампы, подписи.
# В штампе стоят БИК и ИНН самого банка — в реквизиты сторон им нельзя.
PODVAL_RE = re.compile(
    r"^\s*(Назначение\s+платежа|Подписи|Отметки\s+банка|М\.\s*П\.|Штамп)", re.IGNORECASE
)

# Пробел между цифрами одного числа. В бланке платёжки номера печатают
# вразрядку, чтобы попасть в клетки, и разбор видит «616 125 855 326».
PROBELY_V_CHISLE_RE = re.compile(r"(?<=\d)[ \t ]+(?=\d)")


def _nayti(vyrazhenie: re.Pattern, *teksty: str) -> str:
    """Первое совпадение в любом из видов текста: как напечатано и склеенном."""
    for text in teksty:
        nayden = vyrazhenie.search(text)
        if nayden:
            return nayden.group(1)
    return ""


def _chisla(text: str) -> list[str]:
    """Цепочки цифр документа, в порядке появления."""
    return re.findall(r"\d+", PROBELY_V_CHISLE_RE.sub("", text))


def _oskolki(chisla: list[str], dlina: int) -> list[str]:
    """Числа нужной длины: сперва целые, потом начала и концы длинных.

    При склейке разрядки соседние клетки слипаются: в присланной платёжке ИНН
    стоял вплотную к КПП, и `616125855326` + `0` дали тринадцать цифр, в которых
    ИНН уже не виден. Настоящее значение — начало такой цепочки.

    Осколки идут после целых чисел намеренно: обрывок длинного номера может
    случайно сойтись по контрольной сумме, и целое число всегда правдоподобнее.
    """
    tselye = [c for c in chisla if len(c) == dlina]
    kraya = []
    for c in chisla:
        if len(c) > dlina:
            kraya += [c[:dlina], c[-dlina:]]
    return tselye + kraya


def po_kontrolnym_summam(text: str) -> dict:
    """Опознать реквизиты по контрольным суммам, не глядя на подписи.

    Поиск по метке («ИНН 7719617469») работает, только когда подпись стоит
    рядом со значением. В бланке платёжки это не гарантировано: подпись клетки
    и её содержимое — разные надписи, и при чтении они разъезжаются по разным
    строкам. В одной присланной платёжке подпись «ИНН» оказалась через три
    строки от самого номера.

    Но реквизиты самопроверяемы: у ИНН, БИК, расчётного и корр. счёта есть
    контрольные суммы. Значит число можно опознать по нему самому. Случайное
    число нужной длины сойдётся по контрольной сумме примерно раз на сотню,
    а нужной длины, в нужном месте документа и с нужным префиксом — практически
    никогда.

    Берём первое подходящее: в форме 0401060 плательщик напечатан выше
    получателя, а сюда попадает только верхняя половина документа.
    """
    from generator.validate import check_account, check_bic, check_inn

    chisla = _chisla(text)

    inn = next(
        (c for c in _oskolki(chisla, 12) + _oskolki(chisla, 10) if check_inn(c)),
        "",
    )

    # БИК проверяется слабо — девять цифр сходятся у многих чисел, в том числе
    # у начала ИНН. Поэтому берём не первый подходящий, а тот, под которым
    # сходятся счета: ключ расчётного счёта считается вместе с БИК, и чужой
    # БИК его не подтвердит.
    dvadtsatiznachnye = _oskolki(chisla, 20)
    lucshee = {"bic": "", "account": "", "corr_account": ""}
    for kandidat in _oskolki(chisla, 9):
        if not check_bic(kandidat):
            continue
        schet = next(
            (c for c in dvadtsatiznachnye if not c.startswith("301") and check_account(c, kandidat)),
            "",
        )
        korr = next(
            (
                c
                for c in dvadtsatiznachnye
                if c.startswith("301") and check_account(c, kandidat, corr=True)
            ),
            "",
        )
        if schet and korr:  # оба счёта сошлись — это точно он
            lucshee = {"bic": kandidat, "account": schet, "corr_account": korr}
            break
        if (schet or korr) and not lucshee["bic"]:
            lucshee = {"bic": kandidat, "account": schet, "corr_account": korr}

    return {"inn": inn, **lucshee}


def _sverit(pole: str, znachenie: str, bic: str) -> bool:
    """Сходится ли значение по своей контрольной сумме."""
    from generator.validate import check_account, check_bic, check_inn

    if not znachenie:
        return False
    if pole == "inn":
        return check_inn(znachenie)
    if pole == "bic":
        return check_bic(znachenie)
    if not bic:
        return False
    return check_account(znachenie, bic, corr=(pole == "corr_account"))


def _razdelit(stroki: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(половина плательщика, половина получателя, подвал с отметками банка).

    В форме 0401060 сверху плательщик и его банк, ниже банк получателя
    и получатель, под назначением платежа — отметки банка. Подписи клеток
    стоят под содержимым, поэтому половина получателя начинается с подписи
    «Банк плательщика», а половина плательщика кончается перед «Банк получателя»
    (клетки банка получателя в неё попадают, но контрольные суммы отсеют их).
    """
    granitsa = next(
        (n for n, s in enumerate(stroki) if GRANITSA_RE.search(s)), len(stroki)
    )
    nachalo = next(
        (n for n, s in enumerate(stroki) if NACHALO_POLUCHATELYA_RE.search(s)), granitsa
    )
    konets = next((n for n, s in enumerate(stroki) if PODVAL_RE.match(s)), len(stroki))
    return stroki[:granitsa], stroki[nachalo:konets], stroki[konets:]


def _bik_v_otmetkah(podval: list[str]) -> set[str]:
    """БИК банка, выдавшего документ, — из отметок и штампа внизу."""
    from generator.validate import check_bic

    text = "\n".join(podval)
    bez_probelov = PROBELY_V_CHISLE_RE.sub("", text)
    po_metke = {b for b in BIC_RE.findall(text) + BIC_RE.findall(bez_probelov) if check_bic(b)}
    # Корр. счёт банка в штампе («к/сч 30101810000000000752») тоже выдаёт его БИК
    po_korrschetu = {
        s[-3:] for s in SCHET_RE.findall(bez_probelov) if s.startswith("301")
    }
    return po_metke, po_korrschetu


def _vybrat_storonu(
    platelshchik: dict, poluchatel: dict | None, podval: list[str]
) -> tuple[dict, dict | None, str]:
    """Чья это платёжка — нашей организации как плательщика или как получателя.

    Документ выдаёт банк своему клиенту. Если в отметках банка внизу стоит
    БИК банка получателя — платёжку выгрузили из банка получателя, значит
    наша организация получила деньги, а не заплатила. Совпал БИК банка
    плательщика или признаков нет — берём плательщика: это обычный случай.
    """
    if poluchatel is None:
        return platelshchik, None, ""
    biki, hvosty_korr = _bik_v_otmetkah(podval)

    def vydan(storona: dict) -> bool:
        bic = storona.get("bic", "")
        return bool(bic) and (bic in biki or (not biki and bic[-3:] in hvosty_korr))

    za_platelshchika, za_poluchatelya = vydan(platelshchik), vydan(poluchatel)
    if za_poluchatelya and not za_platelshchika:
        return (
            poluchatel,
            platelshchik,
            f"документ выдан банком получателя (БИК {poluchatel['bic']} в отметках банка), "
            "то есть по этой платёжке деньги получили вы",
        )
    if za_platelshchika and not za_poluchatelya:
        return platelshchik, poluchatel, "документ выдан банком плательщика"
    return platelshchik, poluchatel, "в платёжке ваша организация обычно плательщик"


def iz_dokumenta(put: str | Path) -> dict:
    """Реквизиты своей организации из платёжки в PDF или снимком.

    Читаются обе стороны; какая из них наша — см. `_vybrat_storonu`. Вторая
    сторона возвращается в `_drugaya_storona`, чтобы человек мог переключить.
    """
    from parser.rows import ExtractionError, read_rows

    try:
        stroki = read_rows(put)
    except ExtractionError as exc:
        raise NeRazobrano(str(exc)) from exc
    if not stroki:
        raise NeRazobrano("в документе не нашлось текста")

    verh, niz, podval = _razdelit(stroki)
    if not verh:
        raise NeRazobrano("не похоже на платёжное поручение: не нашлась часть плательщика")

    platelshchik = _rekvizity_storony(verh, 0)
    if not (platelshchik["inn"] and platelshchik["account"]):
        raise NeRazobrano(
            "в документе не нашлись ИНН и расчётный счёт плательщика — "
            "это точно платёжное поручение?"
        )
    poluchatel = _rekvizity_storony(niz, 1) if niz else None
    if poluchatel is not None and not (
        poluchatel["inn"] and poluchatel["account"] and poluchatel["bic"]
    ):
        poluchatel = None

    itog, drugaya, pochemu = _vybrat_storonu(platelshchik, poluchatel, podval)
    return {**itog, "_drugaya_storona": drugaya, "_pochemu": pochemu}


def _rekvizity_storony(stroki: list[str], storona: int) -> dict:
    """Реквизиты одной стороны платёжки из её половины бланка."""
    text = "\n".join(stroki)
    # Запасной вид документа: пробелы внутри чисел убраны. В платёжках номера
    # печатают вразрядку, чтобы попасть в клетки бланка, и разбор видит
    # «616 125 855 326» вместо ИНН. Ищем сперва как есть, потом в склеенном.
    bez_probelov = PROBELY_V_CHISLE_RE.sub("", text)

    scheta = SCHET_RE.findall(text) or SCHET_RE.findall(bez_probelov)
    po_metkam = {
        "inn": _nayti(INN_RE, text, bez_probelov),
        "bic": _nayti(BIC_RE, text, bez_probelov),
        "account": next((s for s in scheta if not s.startswith("301")), ""),
        "corr_account": next((s for s in scheta if s.startswith("301")), ""),
    }
    zapasnye = po_kontrolnym_summam(text)

    # Подпись — только подсказка, решает контрольная сумма. Оторванная от
    # значения подпись цепляет что попало: в одной платёжке «БИК» на своей
    # строке подцепил первые девять цифр корр. счёта, стоявшего ниже.
    opoznano: dict[str, str] = {}
    bankovskie = ("bic", "account", "corr_account")
    if all(zapasnye.get(pole) for pole in bankovskie):
        # БИК и оба счёта подтвердили друг друга — этой тройке верим целиком.
        # Проверять их порознь нельзя: девять цифр сходятся у многих чисел,
        # и одинокий БИК по подписи проходит проверку, будучи чужим.
        opoznano.update({pole: zapasnye[pole] for pole in bankovskie})
    else:
        for pole in bankovskie:
            kandidat = po_metkam[pole]
            podhodit = _sverit(pole, kandidat, opoznano.get("bic", ""))
            opoznano[pole] = kandidat if podhodit else zapasnye.get(pole, "")

    opoznano["inn"] = (
        po_metkam["inn"] if _sverit("inn", po_metkam["inn"], "") else zapasnye.get("inn", "")
    )

    itog = {
        **PUSTO,
        **opoznano,
        "kpp": _nayti(KPP_RE, text, bez_probelov),
        "full_name": _naimenovanie(stroki, PODPISI_STORON[storona]),
        "_storona": STORONY[storona],
    }
    itog["bank_name"], itog["bank_city"] = _bank(stroki, PODPISI_BANKOV[storona])
    itog["name"] = _kratkoe(itog["full_name"])
    return itog


PODPISI_STORON = ("Плательщик", "Получатель")
PODPISI_BANKOV = (r"Банк\s+плательщика", r"Банк\s+получателя")


# Клетки формы стоят рядами, и в одну строку попадает и название, и содержимое
# соседней клетки справа: «ООО "Банк Точка" г. Москва БИК 044525104».
# Поэтому не выбрасываем такую строку целиком, а срезаем хвост соседей.
# Границу слова ставим только в начале каждой метки. После «№» её нет: это
# не буква и не цифра, поэтому \b там не срабатывает, и хвост «Сч. № 40802…»
# оставался в наименовании.
HVOSTY_RE = re.compile(
    rf"(?:(?<![{BUKVA}])(?:БИК|ИНН|КПП)(?![{BUKVA}])|\bСч\.\s*№?|\bСумма\b(?:\s+прописью)?"
    r"|\bВид\s+оп\.|\bСрок\s+плат\.|\bОчер\.\s*плат\.).*$",
    re.IGNORECASE,
)

# Подписи клеток: до них поднимаемся, но внутрь названия они не входят.
# Вторая группа — обрывки подписей, которые бланк печатает отдельной строкой
# («Сумма» / «прописью»): сами по себе они на название не тянут.
PODPISI_RE = re.compile(
    r"^\s*(Плательщик|Получатель|Банк\s+плательщика|Банк\s+получателя|"
    r"ПЛАТ[ЕЁ]ЖНОЕ\s+ПОРУЧЕНИЕ|Дата|Поступ\.|Списано|Назначение"
    # клетки над подписью «Получатель»: «Вид оп. 01 Срок плат.», «Очер. 5»,
    # «Наз. пл.», «Код Рез.поле» — названием не являются
    r"|Вид\s+оп\.|Очер\.|Наз\.\s*пл\.|Код\b|Рез\."
    r"|(?:прописью|Сумма|Вид\s+платежа|Электронно|Срочно)\s*$)",
    re.IGNORECASE,
)


# Номер, набранный вразрядку, в конце строки: «…г. Москва 0 4 4 5 2 5 1 0 4».
# Подпись такой клетки уехала в другую строку, поэтому по метке хвост не срезать.
NOMER_V_KONTSE_RE = re.compile(r"(?:\s*\d){6,}\s*$")


def _bez_hvostov(stroka: str) -> str:
    bez = HVOSTY_RE.sub("", stroka)
    return NOMER_V_KONTSE_RE.sub("", bez).strip(" |[]")


def _pohozhe_na_nazvanie(stroka: str) -> bool:
    """Похоже на название, а не на цифры, подпись клетки или сумму прописью."""
    if not stroka or len(stroka) <= 3 or not re.search(r"[А-Яа-яA-Za-z]{3}", stroka):
        return False
    return not SUMMA_PROPISYU_RE.search(stroka)


def _nazvanie_pod(stroki: list[str], nomer: int) -> str:
    """То же, но вниз от подписи: в части бланков она стоит над клеткой."""
    chasti: list[str] = []
    for vpered in range(nomer + 1, len(stroki)):
        kandidat = _bez_hvostov(stroki[vpered])
        if not _pohozhe_na_nazvanie(kandidat) or PODPISI_RE.match(kandidat):
            if chasti:
                break
            continue
        chasti.append(kandidat)
    return " ".join(chasti).strip()


def _nazvanie_nad(stroki: list[str], nomer: int) -> str:
    """Название из клетки над подписью.

    Поднимаемся вверх: сначала перешагиваем строки, от которых после срезки
    хвостов ничего не осталось (клетки со счетами), затем собираем подряд идущие
    строки названия. Наименование ИП в платёжку не влезает в одну строку —
    «Индивидуальный предприниматель Шульмина Надежда / Алексеевна», — и взять
    только последнюю значило бы записать в справочник «Алексеевна».
    """
    chasti: list[str] = []
    for nazad in range(nomer - 1, -1, -1):
        kandidat = _bez_hvostov(stroki[nazad])
        if not _pohozhe_na_nazvanie(kandidat) or PODPISI_RE.match(kandidat):
            if chasti:
                break  # название кончилось
            continue  # до названия ещё не дошли
        chasti.insert(0, kandidat)
    return " ".join(chasti).strip()


# Похоже на название организации, а не на случайный текст рядом
# Длинные формы стоят первыми: поиск находит самое левое совпадение, и
# «Индивидуальный предприниматель» должно опознаваться с первого слова,
# иначе название обрежется до «предприниматель Шульмина…».
ORGANIZATSIYA_RE = re.compile(
    r"\b(Индивидуальный\s+предприниматель|Общество\s+с\s+ограниченной"
    r"|Публичное\s+акционерное|Закрытое\s+акционерное|Акционерное\s+общество"
    r"|ООО|АО|ПАО|ЗАО|НАО|ИП|Общество|предприниматель|организация)\b",
    re.IGNORECASE,
)

# Обрывки соседних клеток перед названием: «прописью ИНН 7719617469 КПП»
OBRYVKI_RE = re.compile(r"\d|(ИНН|КПП|БИК|Сумма|прописью|Сч)", re.IGNORECASE)

# Сумма прописью стоит в соседней клетке и наименованием не является
SUMMA_PROPISYU_RE = re.compile(r"\bрубл\w*\b.*\bкопе\w*\b", re.IGNORECASE)


def _naimenovanie(stroki: list[str], podpis: str = "Плательщик") -> str:
    """Наименование стороны из её клетки — той, что подписана `podpis`.

    Подпись клетки может стоять и под содержимым, и над ним — зависит от того,
    как банк свёрстывал бланк и в каком порядке текст лёг в PDF. Поэтому
    смотрим по обе стороны от подписи и выбираем то, что похоже на название
    организации.
    """
    for nomer, stroka in enumerate(stroki):
        if not re.fullmatch(rf"\s*{podpis}\s*", stroka, re.IGNORECASE):
            continue
        sverhu = _nazvanie_nad(stroki, nomer)
        snizu = _nazvanie_pod(stroki, nomer)
        # Название организации узнаваемо по форме собственности — ему и верим.
        # Всё, что собралось ДО формы собственности («прописью ИНН… КПП»), —
        # обрывки соседних клеток, а не часть названия: отрезаем.
        for kandidat in (sverhu, snizu):
            nayden = ORGANIZATSIYA_RE.search(kandidat or "")
            if not nayden:
                continue
            pered = kandidat[: nayden.start()]
            # Форма собственности бывает и в конце: «ЭР СОФТ, ООО». Тогда
            # перед ней — само название, и резать его нельзя. Режем, только
            # если впереди мусор: цифры или подписи соседних клеток.
            if pered.strip() and not OBRYVKI_RE.search(pered):
                return kandidat.strip()
            return kandidat[nayden.start() :].strip()
        return sverhu or snizu

    # Подписи не нашлось — берём первую строку, похожую на название организации
    for stroka in stroki:
        if ORGANIZATSIYA_RE.search(stroka):
            return _bez_hvostov(stroka)
    return ""


def _bank(stroki: list[str], podpis: str = r"Банк\s+плательщика") -> tuple[str, str]:
    """Банк стороны и город. В форме они напечатаны одной строкой."""
    for nomer, stroka in enumerate(stroki):
        if re.search(podpis, stroka, re.IGNORECASE):
            return _razdelit_gorod(_nazvanie_nad(stroki, nomer))
    return "", ""


GOROD_RE = re.compile(r"\s*(\bг\.?\s*[А-ЯЁ][А-Яа-яё\-\s]*)$", re.IGNORECASE)


def _razdelit_gorod(bank: str) -> tuple[str, str]:
    """«ООО "Банк Точка" г. Москва» -> банк и город отдельно.

    В файле для клиент-банка это разные поля, ПлательщикБанк1 и ПлательщикБанк2.
    Написание сохраняем как в документе: банки печатают и «г. Москва»,
    и «Г. МОСКВА», и переписывать за ними мы не нанимались.
    """
    nayden = GOROD_RE.search(bank)
    if not nayden:
        return bank.strip(), ""
    gorod = re.sub(r"\s+", " ", nayden.group(1)).strip()
    return bank[: nayden.start()].strip(), gorod


def _kratkoe(polnoe: str) -> str:
    """«Общество с ограниченной ответственностью "Ромашка"» -> «ООО "Ромашка"»."""
    if not polnoe:
        return ""
    zamena = {
        "общество с ограниченной ответственностью": "ООО",
        "акционерное общество": "АО",
        "публичное акционерное общество": "ПАО",
        "закрытое акционерное общество": "ЗАО",
        "индивидуальный предприниматель": "ИП",
    }
    nizhniy = polnoe.lower()
    for dlinnoe, korotkoe in zamena.items():
        if nizhniy.startswith(dlinnoe):
            return f"{korotkoe} {polnoe[len(dlinnoe):].strip()}".strip()
    return polnoe.strip()


# --- общий вход ---


# --- счёт на оплату ---

KPP_RYADOM_RE = r"{inn}\D{{0,30}}\bКПП\b[:\s]*(\d{{9}})"

# Метки, по которым в счёте ищется тот, кто платит
METKI_POKUPATELYA = ("Покупатель", "Заказчик", "Плательщик", "Клиент")


def _pochemu_ne_vyshlo(stroki: list[str]) -> str:
    """Сказать, что программа увидела в документе.

    Голое «не нашлись реквизиты» ставит человека в тупик: непонятно, то ли
    файл не тот, то ли программа не справилась. Поэтому показываем, что
    прочиталось, — по этому обычно сразу видно, в чём дело.
    """
    soderzhatelnye = [s.strip() for s in stroki if len(s.strip()) > 10]
    nachalo = soderzhatelnye[0][:80] if soderzhatelnye else ""

    est_metka = any(
        any(m.lower() in s.lower() for m in METKI_POKUPATELYA) for s in stroki
    )
    innov = len(set(re.findall(r"\b\d{10}\b|\b\d{12}\b", "\n".join(stroki))))

    chasti = ["не нашлось, кто платит"]
    if not soderzhatelnye:
        chasti.append(
            "в файле не оказалось текста — похоже, это скан, сохранённый в PDF. "
            "Сохраните его картинкой (JPG или PNG) и загрузите ещё раз"
        )
        return ". ".join(chasti)

    chasti.append(f"прочитано строк: {len(stroki)}, документ начинается с «{nachalo}»")
    if not est_metka:
        chasti.append(
            "в тексте нет ни «Покупатель», ни «Заказчик», ни «Плательщик» — "
            "по этим меткам и ищется плательщик. Похоже, это документ другого вида"
        )
    elif not innov:
        chasti.append("в документе вообще не нашлось ни одного ИНН")
    else:
        chasti.append(
            f"ИНН в документе есть ({innov} шт.), но не удалось понять, "
            "который из них ваш — впишите реквизиты руками"
        )
    return ". ".join(chasti)


def iz_scheta(put: str | Path) -> dict:
    """Кто платит — из счёта, выставленного на эту организацию.

    Из счёта берётся только то, что в нём напечатано: наименование, ИНН и КПП
    покупателя. **Банковских реквизитов плательщика в счёте нет** — там стоят
    реквизиты того, кому платят. Дописать их можно только из платёжки, выписки
    или руками.
    """
    from parser.parse import parse_invoice
    from parser.rows import ExtractionError, read_rows

    try:
        razobrano = parse_invoice(put)
        stroki = read_rows(put)
    except ExtractionError as exc:
        sovet = ""
        if "текстового слоя" in str(exc):
            # Скан, сохранённый в PDF: распознавание включается только для
            # картинок, внутрь PDF мы пока не заглядываем
            sovet = ". Сохраните этот документ картинкой (JPG или PNG) и загрузите ещё раз"
        raise NeRazobrano(f"{exc}{sovet}") from exc

    inn = str(razobrano.data.get("_buyer_inn") or "").strip()
    imya = str(razobrano.data.get("_buyer_name") or "").strip()
    if not inn:
        raise NeRazobrano(_pochemu_ne_vyshlo(stroki), stroki)

    kpp = ""
    nayden = re.search(KPP_RYADOM_RE.format(inn=re.escape(inn)), "\n".join(stroki))
    if nayden:
        kpp = nayden.group(1)

    return {
        **PUSTO,
        "inn": inn,
        "kpp": kpp,
        "full_name": imya,
        "name": _kratkoe(imya),
        "_istochnik": "schet",
    }


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def kod_iz_imeni(name: str) -> str:
    """Подсказка кода организации: он идёт в имя файла для банка, поэтому латиницей."""
    bez_formy = re.sub(r"^(ООО|АО|ПАО|ЗАО|ИП)\b", "", name, flags=re.IGNORECASE).strip()
    bukvy = [TRANSLIT.get(s, s if s.isalnum() and s.isascii() else "-") for s in bez_formy.lower()]
    kod = re.sub(r"-+", "-", "".join(bukvy)).strip("-")
    return kod[:32]


def razobrat(imya_fayla: str, dannye: bytes, vremenny_put: str | Path) -> dict:
    """Разобрать что дали: файл обмена, платёжку или счёт.

    Человек не обязан разбираться, какой документ «правильный». Он кладёт то,
    что у него есть, а мы берём оттуда что можем и честно говорим, чего не хватает.
    """
    if pohozhe_na_obmen(dannye):
        return {**iz_obmena(dannye), "_istochnik": "obmen"}
    if Path(imya_fayla).suffix.lower() in {".txt", ".csv"}:
        raise NeRazobrano(
            "текстовый файл не похож на выгрузку клиент-банка "
            f"(в начале нет строки «{MARKER}»)"
        )
    try:
        return {**iz_dokumenta(vremenny_put), "_istochnik": "platezhka"}
    except NeRazobrano:
        # Не платёжка — попробуем счёт: в нём есть кто платит, но нет, куда
        return iz_scheta(vremenny_put)


NE_HVATAET = ("account", "bank_name", "bic", "corr_account")


def chego_ne_hvataet(dannye: dict) -> list[str]:
    """Каких банковских реквизитов не набралось из документа."""
    nazvaniya = {
        "account": "расчётный счёт",
        "bank_name": "банк",
        "bic": "БИК",
        "corr_account": "корр. счёт",
    }
    return [nazvaniya[p] for p in NE_HVATAET if not str(dannye.get(p) or "").strip()]

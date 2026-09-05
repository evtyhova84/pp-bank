<#
    Резервная копия сервиса «Счета в банк».

    Запускается сам каждую ночь (задание «Scheta-kopiya», его заводит
    ustanovka.ps1). Вручную:
        powershell -ExecutionPolicy Bypass -File C:\pp-bank\razvertyvanie\kopiya.ps1

    Что копируется и почему:
        servis\dannye\servis.db  — люди, плательщики, ЖУРНАЛ НУМЕРАЦИИ платёжек.
                                   Потеряется — счётчик начнёт заново, и в банк
                                   уйдут платёжки с уже использованными номерами.
        servis\dannye\sekret.key — ключ подписи форм
        servis\dannye\vygruzki   — сформированные файлы для банка
        servis\dannye\zagruzki   — исходные счета (объёмная часть)

    База копируется через встроенный механизм SQLite, а не как файл: сервис
    работает в режиме WAL, и простое копирование даёт битую копию.
#>
param(
    [string]$Papka = "C:\pp-bank",
    [string]$Kuda = "",
    # Сколько ночных копий держим
    [int]$Hranit = 14,
    # Исходные счета — самое объёмное. По умолчанию копируем их раз в неделю.
    [switch]$SoSchetami
)

$ErrorActionPreference = "Stop"

$dannye = Join-Path $Papka "servis\dannye"
if (-not (Test-Path (Join-Path $dannye "servis.db"))) {
    Write-Error "не нашёл базу: $dannye\servis.db"
}
if (-not $Kuda) { $Kuda = Join-Path $Papka "kopii" }

$metka = Get-Date -Format "yyyy-MM-dd-HHmm"
$papka = Join-Path $Kuda $metka
New-Item -ItemType Directory -Path $papka -Force | Out-Null

# --- база ---
# .backup вместо копирования файла: снимок целостный даже под нагрузкой.
$baza = Join-Path $papka "servis.db"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($python) {
    $kod = @"
import sqlite3, sys
istok = sqlite3.connect(sys.argv[1])
kopiya = sqlite3.connect(sys.argv[2])
with kopiya:
    istok.backup(kopiya)
kopiya.close(); istok.close()
"@
    $vremenno = Join-Path $env:TEMP "kopiya-bazy.py"
    Set-Content -Path $vremenno -Value $kod -Encoding UTF8
    & $python $vremenno (Join-Path $dannye "servis.db") $baza
    Remove-Item $vremenno -Force
} elseif (Get-Command sqlite3 -ErrorAction SilentlyContinue) {
    sqlite3 (Join-Path $dannye "servis.db") ".backup '$baza'"
} else {
    Write-Error "нет ни python, ни sqlite3 — нечем снять целостную копию базы"
}

# --- ключ и выгрузки ---
Copy-Item (Join-Path $dannye "sekret.key") $papka -ErrorAction SilentlyContinue
$vygruzki = Join-Path $dannye "vygruzki"
if (Test-Path $vygruzki) {
    robocopy $vygruzki (Join-Path $papka "vygruzki") /E /NFL /NDL /NJH /NJS /NP | Out-Null
}

# --- исходные счета: по воскресеньям или по флагу ---
if ($SoSchetami -or (Get-Date).DayOfWeek -eq "Sunday") {
    $zagruzki = Join-Path $dannye "zagruzki"
    if (Test-Path $zagruzki) {
        robocopy $zagruzki (Join-Path $papka "zagruzki") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    }
}

# --- убрать старые ---
Get-ChildItem $Kuda -Directory |
    Sort-Object Name -Descending |
    Select-Object -Skip $Hranit |
    Remove-Item -Recurse -Force

$mb = [math]::Round(((Get-ChildItem $papka -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "Копия готова: $papka ($mb МБ). Храним последние $Hranit."
Write-Host "ВАЖНО: копия лежит на том же сервере. Раз в неделю уносите папку $Kuda наружу."

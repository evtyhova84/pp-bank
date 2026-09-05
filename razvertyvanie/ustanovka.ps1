<#
    Установка сервиса «Счета в банк» на Windows-сервер.

    Запускать НА СЕРВЕРЕ, в PowerShell от имени администратора:
        powershell -ExecutionPolicy Bypass -File C:\pp-bank\razvertyvanie\ustanovka.ps1 -Domen scheta.vashdomen.ru

    Что ставится (всё без AGPL, как договорились по проекту):
        Python 3.13             PSF           — сам сервис
        Tesseract OCR           Apache 2.0    — второй движок распознавания сканов
        NSSM                    public domain — запуск сервиса службой Windows
        Caddy                   Apache 2.0    — HTTPS и сертификат Let's Encrypt
        языковой пакет OCR ru   часть Windows — основной движок распознавания

    Скрипт можно запускать повторно: уже сделанное он пропускает.
#>
param(
    # Домен, по которому сотрудники будут заходить. Его A-запись должна уже
    # указывать на этот сервер, иначе Caddy не получит сертификат.
    [Parameter(Mandatory = $true)][string]$Domen,
    # Куда распакован проект
    [string]$Papka = "C:\pp-bank",
    # Порт, на котором слушает сам сервис. Наружу он не выставляется.
    [int]$Port = 8000,
    [switch]$BezVoprosov
)

$ErrorActionPreference = "Stop"

function Shag($tekst) {
    Write-Host ""
    Write-Host "==> $tekst" -ForegroundColor Cyan
}

function Sprosit($tekst) {
    if ($BezVoprosov) { return $true }
    $otvet = Read-Host "$tekst [д/н]"
    return $otvet -match "^[дdyY]"
}

# --- 0. Проверки до того, как что-то менять ---

$ya = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $ya.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Нужны права администратора: запустите PowerShell «от имени администратора»."
}
if (-not (Test-Path (Join-Path $Papka "servis\app\main.py"))) {
    Write-Error "В $Papka нет проекта (не найден servis\app\main.py). Распакуйте архив туда или укажите -Papka."
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Error "Нет winget. На Windows Server поставьте App Installer из Microsoft Store либо ставьте Python, Tesseract, NSSM и Caddy вручную — ссылки в razvertyvanie\README.md."
}

Write-Host "Сервис «Счета в банк»" -ForegroundColor Green
Write-Host "  проект : $Papka"
Write-Host "  домен  : $Domen"
Write-Host "  порт   : 127.0.0.1:$Port (наружу не выставляется)"
if (-not (Sprosit "Продолжаем?")) { exit 1 }

function Obnovit-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Postavit($id, $imya, $proverka) {
    if (& $proverka) { Write-Host "  $imya — уже есть, пропускаю"; return }
    Write-Host "  ставлю $imya ($id)..."
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent
    Obnovit-Path
    if (-not (& $proverka)) { Write-Error "$imya не встал. Поставьте вручную и запустите скрипт заново." }
}

# --- 1. Python и библиотеки ---

Shag "Python и библиотеки"
Postavit "Python.Python.3.13" "Python" { [bool](Get-Command python -ErrorAction SilentlyContinue) }

$python = (Get-Command python).Source
Write-Host "  python: $python"
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r (Join-Path $Papka "servis\requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "не поставились библиотеки из servis\requirements.txt" }
Write-Host "  библиотеки на месте"

# --- 2. Распознавание сканов ---

Shag "Распознавание сканов"

# Основной движок — встроенный Windows.Media.Ocr. На Windows Server русского
# языка для него по умолчанию нет, и без него распознавание счетов бесполезно.
$ocr = Get-WindowsCapability -Online -Name "Language.OCR~~~ru-RU~0.0.1.0" -ErrorAction SilentlyContinue
if ($ocr -and $ocr.State -ne "Installed") {
    Write-Host "  ставлю русский язык для встроенного OCR..."
    Add-WindowsCapability -Online -Name "Language.OCR~~~ru-RU~0.0.1.0" | Out-Null
} elseif ($ocr) {
    Write-Host "  русский OCR Windows — уже есть"
} else {
    Write-Warning "  русского OCR Windows на этой сборке нет; останется только Tesseract (на эталоне 24 ключевых поля из 40 против 33)"
}

Postavit "UB-Mannheim.TesseractOCR" "Tesseract" {
    [bool](Get-Command tesseract -ErrorAction SilentlyContinue) -or
    (Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe")
}

Push-Location (Join-Path $Papka "2-raspoznavanie")
& $python -c "from parser import ocr; print('  движки:', ocr.dostupnye_dvizhki())"
Pop-Location

# --- 3. Служба сервиса ---

Shag "Служба Windows"
Postavit "NSSM.NSSM" "NSSM" { [bool](Get-Command nssm -ErrorAction SilentlyContinue) }

$zhurnal = Join-Path $Papka "servis\dannye\zhurnal"
New-Item -ItemType Directory -Path $zhurnal -Force | Out-Null

$sluzhba = "SchetaVBank"
if (Get-Service $sluzhba -ErrorAction SilentlyContinue) {
    Write-Host "  служба $sluzhba уже заведена — обновляю настройки"
    nssm stop $sluzhba confirm 2>$null | Out-Null
} else {
    nssm install $sluzhba $python "-m servis --host 127.0.0.1 --port $Port"
}
nssm set $sluzhba Application $python | Out-Null
nssm set $sluzhba AppParameters "-m servis --host 127.0.0.1 --port $Port" | Out-Null
nssm set $sluzhba AppDirectory $Papka | Out-Null
nssm set $sluzhba DisplayName "Счета в банк" | Out-Null
nssm set $sluzhba Description "Счета от заказчиков -> файл платёжек для клиент-банка" | Out-Null
nssm set $sluzhba Start SERVICE_AUTO_START | Out-Null
# Журнал службы: без него разбираться в падении на сервере нечем
nssm set $sluzhba AppStdout (Join-Path $zhurnal "sluzhba.log") | Out-Null
nssm set $sluzhba AppStderr (Join-Path $zhurnal "sluzhba.log") | Out-Null
nssm set $sluzhba AppRotateFiles 1 | Out-Null
nssm set $sluzhba AppRotateBytes 10485760 | Out-Null
nssm start $sluzhba | Out-Null
Write-Host "  служба $sluzhba запущена"

# --- 4. HTTPS ---

Shag "HTTPS: Caddy"
Postavit "CaddyServer.Caddy" "Caddy" { [bool](Get-Command caddy -ErrorAction SilentlyContinue) }

$caddyfile = Join-Path $Papka "razvertyvanie\Caddyfile"
$obrazets = Join-Path $Papka "razvertyvanie\Caddyfile.obrazets"
(Get-Content $obrazets -Raw -Encoding UTF8).Replace("scheta.vashdomen.ru", $Domen).Replace("127.0.0.1:8000", "127.0.0.1:$Port") | Set-Content $caddyfile -Encoding UTF8
Write-Host "  настройки: $caddyfile"

$caddy = (Get-Command caddy).Source
if (-not (Get-Service caddy -ErrorAction SilentlyContinue)) {
    nssm install caddy $caddy "run --config `"$caddyfile`""
}
nssm set caddy Application $caddy | Out-Null
nssm set caddy AppParameters "run --config `"$caddyfile`"" | Out-Null
nssm set caddy AppDirectory (Join-Path $Papka "razvertyvanie") | Out-Null
nssm set caddy Start SERVICE_AUTO_START | Out-Null
nssm set caddy AppStdout (Join-Path $zhurnal "caddy.log") | Out-Null
nssm set caddy AppStderr (Join-Path $zhurnal "caddy.log") | Out-Null
nssm restart caddy 2>$null | Out-Null
if ((Get-Service caddy).Status -ne "Running") { nssm start caddy | Out-Null }
Write-Host "  Caddy запущен, сертификат он получит сам при первом обращении"

# --- 5. Брандмауэр ---

Shag "Брандмауэр"
foreach ($pravilo in @(@{ imya = "Scheta HTTP"; port = 80 }, @{ imya = "Scheta HTTPS"; port = 443 })) {
    if (-not (Get-NetFirewallRule -DisplayName $pravilo.imya -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $pravilo.imya -Direction Inbound -Protocol TCP -LocalPort $pravilo.port -Action Allow | Out-Null
    }
    Write-Host "  открыт порт $($pravilo.port)"
}
Write-Host "  порт $Port наружу не открывается — сервис слушает только 127.0.0.1"

# --- 6. Резервные копии ---

Shag "Резервные копии"
$zadanie = "Scheta-kopiya"
if (-not (Get-ScheduledTask -TaskName $zadanie -ErrorAction SilentlyContinue)) {
    $deystvie = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -NoProfile -File `"$Papka\razvertyvanie\kopiya.ps1`""
    $kogda = New-ScheduledTaskTrigger -Daily -At 3am
    Register-ScheduledTask -TaskName $zadanie -Action $deystvie -Trigger $kogda -User "SYSTEM" -RunLevel Highest -Description "Копия базы сервиса «Счета в банк»" | Out-Null
}
Write-Host "  копия базы делается каждую ночь в 3:00 -> $Papka\kopii"

# --- Итог ---

Write-Host ""
Write-Host "Готово." -ForegroundColor Green
Write-Host ""
Write-Host "  Откройте https://$Domen — сервис предложит завести первого администратора."
Write-Host "  Дальше: «Плательщики» -> ваша организация и её расчётный счёт,"
Write-Host "  затем «Сотрудники» -> логины тестировщикам."
Write-Host ""
Write-Host "  Проверить, что живо:  Get-Service SchetaVBank, caddy"
Write-Host "  Журнал сервиса:       $zhurnal\sluzhba.log"
Write-Host "  После обновления кода: Restart-Service SchetaVBank"

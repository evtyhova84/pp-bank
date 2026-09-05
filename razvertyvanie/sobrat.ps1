<#
    Упаковать проект для переноса на сервер.

    Запускать на своей машине, из корня проекта:
        powershell -ExecutionPolicy Bypass -File razvertyvanie\sobrat.ps1

    Получается pp-bank-<дата>.zip рядом с проектом. В архив НЕ попадают:
      servis\dannye  — рабочая база, загруженные счета и ключ подписи форм.
                       На сервере всё это заводится заново, с чистого листа.
      __pycache__    — скомпилированные модули чужой версии Python.
      2-raspoznavanie\scheta, 1-vygruzka-v-bank\platezhki — рабочие папки.
#>
param(
    [string]$Kuda = ""
)

$ErrorActionPreference = "Stop"

$koren = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $koren "servis\app\main.py"))) {
    Write-Error "Скрипт лежит не в проекте: не нашёл servis\app\main.py рядом."
}

$data = Get-Date -Format "yyyy-MM-dd"
if (-not $Kuda) { $Kuda = Join-Path (Split-Path -Parent $koren) "pp-bank-$data.zip" }

# Собираем во временной папке: Compress-Archive не умеет исключать пути.
$vremenno = Join-Path $env:TEMP "pp-bank-sborka-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $vremenno -Force | Out-Null

$iskluchit = @(
    "servis\dannye",
    "2-raspoznavanie\scheta",
    "1-vygruzka-v-bank\platezhki",
    ".git",
    ".claude",
    ".agents"
)

Write-Host "Копирую проект без рабочих данных..."
robocopy $koren $vremenno /E /NFL /NDL /NJH /NJS /NP `
    /XD __pycache__ .git .claude .agents node_modules `
    ($iskluchit | ForEach-Object { Join-Path $koren $_ }) | Out-Null
# robocopy возвращает 0-7 как успех, 8+ как ошибку
if ($LASTEXITCODE -ge 8) { Write-Error "не удалось скопировать проект (robocopy $LASTEXITCODE)" }

# Пустые рабочие папки нужны — сервис в них пишет
foreach ($p in @("servis\dannye", "2-raspoznavanie\scheta", "1-vygruzka-v-bank\platezhki")) {
    New-Item -ItemType Directory -Path (Join-Path $vremenno $p) -Force | Out-Null
}

if (Test-Path $Kuda) { Remove-Item $Kuda -Force }
Compress-Archive -Path (Join-Path $vremenno "*") -DestinationPath $Kuda
Remove-Item $vremenno -Recurse -Force

$mb = [math]::Round((Get-Item $Kuda).Length / 1MB, 1)
Write-Host ""
Write-Host "Готово: $Kuda ($mb МБ)"
Write-Host "Дальше — razvertyvanie\README.md, шаг 3."

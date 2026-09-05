@echo off
rem Открывает окно программы. Консоль не нужна: вся работа в окне и браузере.
cd /d "%~dp0"

where pythonw >nul 2>&1
if errorlevel 1 goto net_pythona

start "" pythonw "%~dp0zapusk.pyw"
exit /b 0

:net_pythona
echo.
echo   Не найден Python. Поставьте его с python.org
echo   и при установке отметьте "Add python.exe to PATH".
echo.
pause
exit /b 1

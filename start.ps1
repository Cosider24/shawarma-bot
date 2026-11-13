$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Создан .env. Заполните BOT_TOKEN, MANAGER_IDS и WEBAPP_URL, затем повторите запуск.'
    exit 1
}
if (!(Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Нужен установленный Python 3.12 или новее' }
}
& .venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }
& .venv\Scripts\python.exe bot.py
exit $LASTEXITCODE

# Starts the admin dashboard on this machine (native, Windows PowerShell).
# From http://127.0.0.1:8000/admin use Control -> Start to launch the bot.
#   .\start-dashboard.ps1            # port 8000
#   .\start-dashboard.ps1 -Port 8001
param([int]$Port = 8000)
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn dashboard.app:app --host 127.0.0.1 --port $Port

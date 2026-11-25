# Starts the admin dashboard on this machine (native, Windows PowerShell).
# From /admin use Control -> Start to launch the bot.
#   .\start-dashboard.ps1            # this PC only:   http://127.0.0.1:8000/admin
#   .\start-dashboard.ps1 -Lan       # also your phone on the same Wi-Fi (prints the address)
#   .\start-dashboard.ps1 -Port 8001
# -Lan serves plain HTTP on your network: the dashboard key is sent unencrypted. Use it
# only on a Wi-Fi you trust, and never port-forward it. For access away from home use a
# private tunnel such as Tailscale instead.
param([int]$Port = 8000, [switch]$Lan)
Set-Location $PSScriptRoot
$bind = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
if ($Lan) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2\d|3[01]))\.' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1).IPAddress
    Write-Host "Phone (same Wi-Fi): http://${ip}:$Port/admin" -ForegroundColor Cyan
    Write-Host "Allow Python through Windows Firewall (Private networks only) if asked." -ForegroundColor Yellow
}
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn dashboard.app:app --host $bind --port $Port

param(
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8000
)

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host " Starting QuickCompare Server on http://${HostAddress}:${Port}" -ForegroundColor Green
Write-Host " Default host: 0.0.0.0 | Default port: 8000" -ForegroundColor Yellow
Write-Host "====================================================" -ForegroundColor Cyan

$env:HOST = $HostAddress
$env:PORT = "$Port"

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($HostAddress), $Port)
try {
    $listener.Start()
} catch {
    Write-Host "Cannot start: port $Port is already in use. Stop the existing server or choose -Port 8001." -ForegroundColor Yellow
    exit 1
} finally {
    $listener.Stop()
}

Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn server:app --host $HostAddress --port $Port

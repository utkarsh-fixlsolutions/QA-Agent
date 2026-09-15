# Launches the QA Agent web server bound to localhost only, for use as a
# Windows Scheduled Task (see docs/40-local-deployment.md). Not for
# interactive dev use - that's `python -m uvicorn web.server:app --reload`
# from web/README.md.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "web\logs"
$logFile = Join-Path $logDir "server.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

Set-Location $root

"---- start $(Get-Date -Format o) ----" | Out-File -FilePath $logFile -Append -Encoding utf8

& $python -m uvicorn web.server:app --host 127.0.0.1 --port 8000 *>> $logFile

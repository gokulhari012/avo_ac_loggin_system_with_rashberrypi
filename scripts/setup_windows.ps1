<#
.SYNOPSIS
  Installs the Temperature Monitoring server on Windows.

.DESCRIPTION
  Creates server\.venv, installs pinned dependencies, creates server\.env with a
  random SECRET_KEY (if missing), creates/upgrades the database, seeds roles and
  creates the first administrator.  Optional: open the Windows Firewall port so
  Raspberry Pi nodes on the LAN can connect.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Lan -Port 8000   # (run as Administrator for the firewall rule)
#>
param(
    [switch]$Lan,              # listen on all interfaces and add a firewall rule (needs admin)
    [int]$Port = 8000,
    [string]$AdminUser = "admin"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Server = Join-Path $Root "server"
$Venv = Join-Path $Server ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

Write-Host "== Temperature Monitoring server setup ==" -ForegroundColor Cyan

# 1. Python
$launcher = Get-Command py -ErrorAction SilentlyContinue
if (-not $launcher) { throw "Python launcher 'py' not found. Install Python 3.11+ from python.org (tick 'Add to PATH')." }
$version = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.10") { throw "Python $version found; 3.10 or newer is required." }
Write-Host "Python $version"

# 2. Virtual environment + dependencies
if (-not (Test-Path $Py)) {
    Write-Host "Creating virtual environment..."
    & py -3 -m venv $Venv
}
& $Py -m pip install --upgrade pip --quiet
& $Py -m pip install -r (Join-Path $Server "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Write-Host "Dependencies installed."

# 3. Configuration file
$EnvFile = Join-Path $Server ".env"
if (-not (Test-Path $EnvFile)) {
    $secret = & $Py -c "import secrets; print(secrets.token_urlsafe(48))"
    $hostValue = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
    (Get-Content (Join-Path $Server ".env.example")) `
        -replace '^SECRET_KEY=.*', "SECRET_KEY=$secret" `
        -replace '^HOST=.*', "HOST=$hostValue" `
        -replace '^PORT=.*', "PORT=$Port" | Set-Content -Encoding utf8 $EnvFile
    # Only the current user and Administrators/SYSTEM may read the secrets file.
    icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" "Administrators:(F)" "SYSTEM:(F)" | Out-Null
    Write-Host "Created server\.env (HOST=$hostValue, PORT=$Port). Review it before going live."
} else {
    Write-Host "server\.env already exists - left unchanged."
}

# 4. Database schema and roles
Push-Location $Server
try {
    & $Py -m flask --app run db upgrade
    if ($LASTEXITCODE -ne 0) { throw "database migration failed" }
    & $Py -m flask --app run seed
    $hasUsers = & $Py -c "from run import app; from app.models import User; ctx = app.app_context(); ctx.push(); print(User.query.count())"
    if ([int]$hasUsers -eq 0) {
        Write-Host "Create the first administrator account:" -ForegroundColor Yellow
        & $Py -m flask --app run create-admin --username $AdminUser
    }
} finally { Pop-Location }

# 5. Firewall (LAN access for Raspberry Pi nodes)
if ($Lan) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        if (-not (Get-NetFirewallRule -DisplayName "Temperature Monitoring Server" -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName "Temperature Monitoring Server" -Direction Inbound -Protocol TCP `
                -LocalPort $Port -Action Allow -Profile Domain,Private | Out-Null
        }
        Write-Host "Firewall: inbound TCP $Port allowed on Domain/Private networks."
    } else {
        Write-Warning "Not running as Administrator: firewall rule NOT created. Re-run elevated or see docs\installation-windows.md."
    }
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "  Start now:            powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1"
Write-Host "  Start at boot:        powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1   (as Administrator)"
Write-Host "  Diagnose problems:    powershell -ExecutionPolicy Bypass -File scripts\diagnose_server.ps1"

<#
.SYNOPSIS
  Runs the server in the foreground (Ctrl+C to stop).  Uses Waitress, not the
  Flask development server.  HOST/PORT come from server\.env.
#>
$ErrorActionPreference = "Stop"
$Server = Join-Path (Split-Path -Parent $PSScriptRoot) "server"
$Py = Join-Path $Server ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "server\.venv not found - run scripts\setup_windows.ps1 first." }
Push-Location $Server
try { & $Py run.py @args } finally { Pop-Location }

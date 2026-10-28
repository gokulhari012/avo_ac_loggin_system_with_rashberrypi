<#
.SYNOPSIS
  Starts the server automatically at boot (Windows Task Scheduler), restarting it
  if it stops.  Run in an elevated PowerShell.

.DESCRIPTION
  Uses only built-in Windows features.  The task runs as SYSTEM at startup,
  before anyone logs in, and is restarted every minute if it exits.
  For a classic Windows service instead, see docs\installation-windows.md (NSSM).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1 -Uninstall
#>
param([switch]$Uninstall, [string]$TaskName = "TemperatureMonitoringServer")
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this script in an elevated (Administrator) PowerShell." }

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task $TaskName."
    return
}

$Server = Join-Path (Split-Path -Parent $PSScriptRoot) "server"
$Py = Join-Path $Server ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "server\.venv not found - run scripts\setup_windows.ps1 first." }

$action = New-ScheduledTaskAction -Execute $Py -Argument "run.py" -WorkingDirectory $Server
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Temperature Monitoring central server (Waitress)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
Write-Host "Scheduled task '$TaskName' installed and started. State: $((Get-ScheduledTask -TaskName $TaskName).State)"
Write-Host "Logs: server\logs\server.log    Diagnose: scripts\diagnose_server.ps1"

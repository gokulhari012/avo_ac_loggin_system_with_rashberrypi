<#
.SYNOPSIS
  Stops the server (scheduled task and any foreground instance of server\run.py).
#>
param([string]$TaskName = "TemperatureMonitoringServer")
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName
    Write-Host "Scheduled task stopped (it starts again at next boot; disable it in Task Scheduler to prevent that)."
}
$venvPython = Join-Path (Split-Path -Parent $PSScriptRoot) "server\.venv\Scripts\python.exe"
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.ExecutablePath -eq $venvPython -and $_.CommandLine -match 'run\.py' }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force
    Write-Host "Stopped server process $($p.ProcessId)"
}
if (-not $task -and -not $procs) { Write-Host "No running server found." }

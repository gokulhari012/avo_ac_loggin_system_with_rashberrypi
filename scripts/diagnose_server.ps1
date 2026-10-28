<#
.SYNOPSIS
  Checks the server installation and prints what is wrong (read-only).
#>
param([string]$TaskName = "TemperatureMonitoringServer")
$Root = Split-Path -Parent $PSScriptRoot
$Server = Join-Path $Root "server"
$Py = Join-Path $Server ".venv\Scripts\python.exe"
$EnvFile = Join-Path $Server ".env"

function Check($label, $ok, $detail) {
    $mark = if ($ok) { "[ OK ]" } else { "[FAIL]" }
    $color = if ($ok) { "Green" } else { "Red" }
    Write-Host ("{0} {1}  {2}" -f $mark, $label, $detail) -ForegroundColor $color
}

$settings = @{ HOST = "127.0.0.1"; PORT = "8000" }
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object {
        $settings[$Matches[1]] = $Matches[2].Trim()
    }
}
$port = [int]$settings.PORT

Check "Python virtualenv" (Test-Path $Py) $Py
Check "Configuration file" (Test-Path $EnvFile) $EnvFile
Check "Bind address" $true ("HOST={0} PORT={1}  ({2})" -f $settings.HOST, $port,
    $(if ($settings.HOST -eq "127.0.0.1") { "this PC only - nodes on the LAN cannot connect" } else { "LAN accessible" }))

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Check "Startup task" ($null -ne $task) $(if ($task) { "state: $($task.State)" } else { "not installed (scripts\install_windows_service.ps1)" })

$listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
Check "Listening on port $port" ($null -ne $listen) $(if ($listen) { "PID $($listen[0].OwningProcess) on $($listen[0].LocalAddress)" } else { "nothing listening" })

try {
    $ping = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/v1/ping" -TimeoutSec 5
    Check "API ping" ($ping.status -eq "ok") "server time $($ping.server_time), version $($ping.version)"
} catch { Check "API ping" $false $_.Exception.Message }

$rule = Get-NetFirewallRule -DisplayName "Temperature Monitoring Server" -ErrorAction SilentlyContinue
Check "Firewall rule" ($null -ne $rule) $(if ($rule) { "enabled: $($rule.Enabled), profiles: $($rule.Profile)" } else { "missing - LAN nodes may be blocked" })

$profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue
foreach ($p in $profiles) {
    Check "Network '$($p.Name)'" ($p.NetworkCategory -ne "Public") "category $($p.NetworkCategory) (rule covers Domain/Private only)"
}

$ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }).IPAddress
Write-Host "       Nodes should use server.url = http://<one of: $($ips -join ', ')>:$port"

$db = Join-Path $Server "instance\temperature.db"
if (Test-Path $db) { Check "Database file" $true ("{0:N1} MB  {1}" -f ((Get-Item $db).Length / 1MB), $db) }
$drive = (Get-Item $Server).PSDrive
Check "Free disk space" ($drive.Free -gt 1GB) ("{0:N1} GB free on {1}:" -f ($drive.Free / 1GB), $drive.Name)

$log = Join-Path $Server "logs\server.log"
if (Test-Path $log) {
    Write-Host "`n--- last 25 lines of $log ---" -ForegroundColor Cyan
    Get-Content $log -Tail 25
} else { Write-Host "No log file yet at $log" }

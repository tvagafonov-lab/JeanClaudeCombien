# monitor_guard.ps1 — external watchdog for JeanClaudeCombien
# ---------------------------------------------------------------------------
# Why this exists (incidents 2026-05 … 2026-06): a monitor process launched
# by the Startup shortcut the instant the user logs in after wake gets its
# TLS throttled by local security software and silently fails every fetch.
# Its OWN watchdog can't help — the respawn is born in the same poisoned
# login window and is equally dead (proven: PID 24968 -> respawn 38944, both
# silent; a process started minutes later in a warm system fetches fine).
#
# This guard runs from Task Scheduler every 5 minutes, OUTSIDE the login
# storm. It restarts the monitor when it is gone or has gone silent (the
# usage-cache file stopped advancing), so the replacement is born in a warm
# context where TLS is not throttled.
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'SilentlyContinue'

$cacheFile = Join-Path $env:APPDATA 'Claude\monitor_usage_cache.json'
$guardLog  = Join-Path $env:APPDATA 'Claude\monitor_guard.log'
$batFile   = 'C:\Users\tvaga\claude_monitor\start_monitor.bat'
$workDir   = 'C:\Users\tvaga\claude_monitor'
$staleMin  = 10   # cache older than this == fetches have stopped

function Write-GuardLog($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try {
        # cap the log at ~64 KB so it never grows unbounded
        if ((Test-Path $guardLog) -and (Get-Item $guardLog).Length -gt 64000) {
            $tail = Get-Content $guardLog -Tail 400
            Set-Content $guardLog $tail -Encoding utf8
        }
        Add-Content -Path $guardLog -Value $line -Encoding utf8
    } catch {}
}

$now = Get-Date

$proc = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'claude_monitor' } |
    Select-Object -First 1

$cacheAgeMin = 999
if (Test-Path $cacheFile) {
    $cacheAgeMin = ($now - (Get-Item $cacheFile).LastWriteTime).TotalMinutes
}

$reason = $null
if (-not $proc) {
    $reason = "no monitor process"
} else {
    $procAgeMin = ($now - $proc.CreationDate).TotalMinutes
    # Only judge a process that has had time to clear the network-readiness
    # gate (up to 5 min) and write a first cache. A young process is left
    # alone so the guard never kills a monitor that is still warming up.
    if ($procAgeMin -gt $staleMin -and $cacheAgeMin -gt $staleMin) {
        $reason = ("silent: proc age {0:N0}m, cache age {1:N0}m" -f $procAgeMin, $cacheAgeMin)
    }
}

if (-not $reason) {
    # Healthy. Stay quiet (don't spam the log every 5 min).
    exit 0
}

Write-GuardLog "restart triggered ($reason)"
if ($proc) {
    Stop-Process -Id $proc.ProcessId -Force
    Write-GuardLog "killed PID $($proc.ProcessId)"
}
Start-Sleep -Milliseconds 700
Start-Process -FilePath $batFile -WorkingDirectory $workDir
Write-GuardLog "launched fresh monitor via start_monitor.bat"

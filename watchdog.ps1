$scriptPath = "C:\Users\jason\HyperliquidTradingAgent\manual_setup.py"
$logPath = "C:\Users\jason\HyperliquidTradingAgent\watcher.log"
$pythonExe = "python"

$watcherArgs = "--asset BTC --direction long --entry 75507 --sl 73997 --tp1 76615,25 --tp2 78384,35 --tp3 79644,20 --trail-sl 78384 --trail-after 2"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "$ts $msg"
}

while ($true) {
    $running = Get-Process python -ErrorAction SilentlyContinue | Where-Object {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "manual_setup"
    }

    if (-not $running) {
        Write-Log "WATCHDOG: manual_setup.py not running. Starting..."
        Start-Process -FilePath $pythonExe -ArgumentList "$scriptPath $watcherArgs" -WindowStyle Normal
        Start-Sleep -Seconds 10
        $check = Get-Process python -ErrorAction SilentlyContinue | Where-Object {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            $cmd -match "manual_setup"
        }
        if ($check) {
            Write-Log "WATCHDOG: Started successfully (PID $($check.Id))"
        } else {
            Write-Log "WATCHDOG: FAILED to start. Will retry in 60s."
        }
    }

    Start-Sleep -Seconds 60
}

# Capture a real Windows-auth TDS login: start tshark, drive Excel into a
# SQL Server connection, stop cleanly. See docs/tds-login-handshake.md for
# what the resulting capture shows and why each field matters.
#
# Output goes to captures/, which is gitignored. The capture contains an
# NTLMv2 challenge and response for a real account. Regenerate it locally
# rather than sharing one.

$ErrorActionPreference = 'Stop'
$repo    = Split-Path $PSScriptRoot -Parent
$outDir  = Join-Path $repo 'captures'
$tshark  = 'C:\Program Files\Wireshark\tshark.exe'
$pcap    = Join-Path $outDir 'excel_winauth.pcapng'
$probe   = Join-Path $PSScriptRoot 'excel_probe.py'
$caplog  = Join-Path $outDir 'tshark.log'

if (-not (Test-Path $tshark)) { Write-Output "FAIL: tshark not found at $tshark"; exit 4 }
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
foreach ($f in @($pcap, $caplog)) { if (Test-Path $f) { Remove-Item $f -Force } }

# Two traps here, both already paid for:
#   $args is a PowerShell automatic variable, so the list needs its own name.
#   Start-Process does not quote array elements, so a capture filter with
#   spaces splits into separate arguments unless it carries its own quotes.
$tsharkArgs = @(
    '-i', '\Device\NPF_Loopback',
    '-p',
    '-f', '"tcp port 1433"',
    '-a', 'duration:45',
    '-w', "`"$pcap`""
)

Write-Output "starting capture -> $pcap"
$cap = Start-Process -FilePath $tshark -ArgumentList $tsharkArgs -PassThru `
                     -WindowStyle Hidden -RedirectStandardError $caplog

# Let the capture bind the adapter before generating traffic.
Start-Sleep -Seconds 3
if ($cap.HasExited) {
    Write-Output "FAIL: tshark exited early (code $($cap.ExitCode))"
    if (Test-Path $caplog) { Write-Output "--- tshark stderr ---"; Get-Content $caplog }
    exit 2
}

Write-Output "running Excel probe..."
& python $probe 2>&1 | Out-String | Write-Output
Write-Output "probe exit code: $LASTEXITCODE"

Write-Output "waiting for capture to close..."
$cap.WaitForExit(90000) | Out-Null
if (-not $cap.HasExited) { Stop-Process -Id $cap.Id -Force; Write-Output "WARN: force-stopped" }

if (-not (Test-Path $pcap)) {
    Write-Output "FAIL: no capture file produced"
    if (Test-Path $caplog) { Write-Output "--- tshark stderr ---"; Get-Content $caplog }
    exit 3
}

Write-Output "capture file: $pcap ($((Get-Item $pcap).Length) bytes)"
Write-Output "packets captured: $((& $tshark -r $pcap 2>$null | Measure-Object -Line).Lines)"

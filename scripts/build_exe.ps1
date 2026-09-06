# Build pysqlbridge.exe and prove it works.
#
#   .\scripts\build_exe.ps1              build and smoke-test
#   .\scripts\build_exe.ps1 -SkipTests   skip the unit suite first
#
# The smoke test is not optional politeness. PyInstaller cannot see imports that
# happen inside a function, so a build can start, listen and load its tables and
# still fail the moment a client authenticates. That is exactly what happened
# with win32timezone, and only running the executable found it.

[CmdletBinding()]
param(
    [switch] $SkipTests,
    [int]    $Port = 1399
)

# Not 'Stop'. PyInstaller and pytest both write progress to stderr, and under
# Stop PowerShell turns the first such line into a terminating NativeCommandError
# and kills a build that was working. Exit codes are the reliable signal from a
# native command, and every call below checks one.
$ErrorActionPreference = 'Continue'
$repo = Split-Path $PSScriptRoot -Parent
Push-Location $repo

try {
    if (-not $SkipTests) {
        Write-Output "running the unit suite..."
        & python -m pytest -q | Select-Object -Last 3
        if ($LASTEXITCODE -ne 0) { Write-Output "tests failed; not building"; exit 1 }
        Write-Output ""
    }

    Write-Output "building..."
    $buildLog = Join-Path $repo "build\pyinstaller.log"
    New-Item -ItemType Directory -Path (Split-Path $buildLog) -Force | Out-Null
    # PyInstaller writes progress to stderr, which PowerShell wraps in error
    # records. Sending it to a file keeps a successful build looking like one.
    & python -m PyInstaller pysqlbridge.spec --noconfirm --clean --log-level WARN 2> $buildLog | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "build failed; see $buildLog"
        Get-Content $buildLog -ErrorAction SilentlyContinue | Select-Object -Last 15
        exit 1
    }

    $exe = Join-Path $repo 'dist\pysqlbridge.exe'
    if (-not (Test-Path $exe)) { Write-Output "no executable produced"; exit 1 }
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Output ""
    Write-Output "built dist\pysqlbridge.exe ($size MB)"
    Write-Output ""

    # Stage somewhere with no source tree, so a build that only works next to
    # its own sources fails here rather than on someone else's machine.
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ("pysqlbridge_smoke_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path (Join-Path $stage 'data') -Force | Out-Null
    Copy-Item $exe -Destination $stage
    Copy-Item (Join-Path $repo 'examples\data\people.csv')  -Destination (Join-Path $stage 'data')
    Copy-Item (Join-Path $repo 'examples\data\cities.json') -Destination (Join-Path $stage 'data')

    # A config without the HTTP table: the smoke test must not need a network.
    @'
{ "tables": [ { "name": "people", "csv": "data/people.csv" } ] }
'@ | Set-Content -Path (Join-Path $stage 'smoke.json') -Encoding utf8

    Write-Output "smoke-testing in $stage"
    $log = Join-Path $stage 'smoke.log'
    $server = Start-Process (Join-Path $stage 'pysqlbridge.exe') `
        -ArgumentList @('--port', "$Port", '--config', 'smoke.json') `
        -WorkingDirectory $stage -PassThru -WindowStyle Hidden -RedirectStandardError $log
    Start-Sleep -Seconds 8

    $failures = @()
    if ($server.HasExited) {
        $failures += "the executable exited immediately"
    } elseif (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        $failures += "nothing is listening on $Port"
    } else {
        try {
            $cs = "Server=tcp:127.0.0.1,$Port;Integrated Security=True;TrustServerCertificate=True;Connect Timeout=15"
            $conn = New-Object System.Data.SqlClient.SqlConnection $cs
            $conn.Open()
            Write-Output "  login:   ok, server reports $($conn.ServerVersion)"

            $cmd = $conn.CreateCommand()
            $cmd.CommandText = 'SELECT name FROM people'
            $reader = $cmd.ExecuteReader()
            $rows = 0
            while ($reader.Read()) { $rows++ }
            $reader.Close()
            if ($rows -lt 1) { $failures += "the query returned no rows" }
            else { Write-Output "  query:   ok, $rows rows" }

            $cmd = $conn.CreateCommand()
            $cmd.CommandText = 'SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES'
            $reader = $cmd.ExecuteReader()
            $listed = 0
            while ($reader.Read()) { $listed++ }
            $reader.Close()
            if ($listed -lt 1) { $failures += "the catalog listed no tables" }
            else { Write-Output "  catalog: ok, $listed table(s)" }

            $conn.Close()
        } catch {
            $failures += "client: $($_.Exception.Message.Split([char]10)[0].Trim())"
        }
    }

    if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }

    Write-Output ""
    if ($failures.Count -gt 0) {
        Write-Output "SMOKE TEST FAILED"
        $failures | ForEach-Object { Write-Output "  $_" }
        if (Test-Path $log) { Write-Output "--- executable log ---"; Get-Content $log }
        exit 1
    }

    Write-Output "smoke test passed. dist\pysqlbridge.exe is ready."
    Write-Output "It needs a config beside it; see examples\tables.json."
} finally {
    Pop-Location
}

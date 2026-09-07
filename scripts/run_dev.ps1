# Launch the working copy for hands-on testing.
#
# Runs the bridge in the foreground with its log on screen, so you can watch a
# client connect, authenticate and query in real time. Ctrl+C stops it.
#
#   .\scripts\run_dev.ps1                     the example tables, 127.0.0.1:1337
#   .\scripts\run_dev.ps1 -Config my.json     your own tables
#   .\scripts\run_dev.ps1 -Port 1400          a different port
#   .\scripts\run_dev.ps1 -NoData             no source, every query errors
#   .\scripts\run_dev.ps1 -Bind 0.0.0.0       reachable from other machines

[CmdletBinding()]
param(
    [int]    $Port = 1337,
    # Not -Host: $Host is a PowerShell automatic variable holding the console
    # UI object, and -Debug is a CmdletBinding common parameter. Both would be
    # redefinitions rather than parameters.
    [string] $Bind = '127.0.0.1',
    [string] $Config,
    [switch] $NoData,
    [switch] $DebugLog
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

# Run against the working copy, not whatever happens to be installed.
# One line on purpose: PowerShell 5.1 mangles the newlines in a here-string
# passed as a native command argument, so a multi-line -c never parses.
$probe = "import importlib.util,pathlib;s=importlib.util.find_spec('pysqlbridge');print(pathlib.Path(s.origin).parents[2] if s else '')"
$installed = (& python -c $probe 2>$null | Out-String).Trim()

if (-not $installed) {
    Write-Output "pysqlbridge is not importable. Installing this checkout in editable mode..."
    & python -m pip install -e "$repo[dev]"
    if ($LASTEXITCODE -ne 0) { Write-Output "install failed"; exit 1 }
} elseif ($installed -ne $repo) {
    Write-Output "WARNING: python imports pysqlbridge from"
    Write-Output "  $installed"
    Write-Output "not from this checkout at"
    Write-Output "  $repo"
    Write-Output "Run: python -m pip install -e `"$repo[dev]`""
    Write-Output ""
}

$version = & python -c "import pysqlbridge; print(pysqlbridge.__version__)" 2>$null
$configPath = if ($Config) { $Config } else { Join-Path $repo 'examples/tables.json' }

Write-Output ""
Write-Output "pySQLbridge $version  listening on $Bind, port $Port"
Write-Output ("-" * 60)
Write-Output "Connect with Windows Authentication. The bridge never sees a"
Write-Output "password; Windows validates the login through SSPI."
Write-Output ""
Write-Output "  Server name for SSMS, Azure Data Studio and sqlcmd."
Write-Output "  The separator is a COMMA. A colon makes the client ignore the"
Write-Output "  port and fail over to Named Pipes."
Write-Output ""
Write-Output "      127.0.0.1,$Port"
Write-Output ""
Write-Output "  Excel:    Data > Get Data > From Database > From SQL Server Database"
Write-Output "  Power BI: Home > Get data > SQL Server"
Write-Output "      Server: 127.0.0.1,$Port      leave Database blank"
Write-Output ""
Write-Output "  .NET connection string:"
Write-Output "      Server=tcp:127.0.0.1,$Port;Integrated Security=True;TrustServerCertificate=True"
Write-Output ""

if ($NoData) {
    Write-Output "No data source: every query returns an error saying so."
} else {
    Write-Output "Serving $configPath"
    Write-Output ""
    Write-Output "  SELECT * FROM people          a CSV file"
    Write-Output "  SELECT TOP 2 * FROM cities    a JSON file"
    Write-Output ""
    Write-Output "  SELECT TOP 5 * FROM pokemon   a live HTTP API"
    Write-Output ""
    Write-Output "Joins, GROUP BY, HAVING, DISTINCT, CTEs, subqueries, UNION,"
    Write-Output "CASE, CAST and 25 scalar functions all answer. Anything not"
    Write-Output "served is refused by name rather than ignored, so a clause"
    Write-Output "that would have changed the rows never quietly does not."
}
Write-Output ""
Write-Output "The certificate is self-signed and generated at startup, so a"
Write-Output "client may need TrustServerCertificate or 'Trust server certificate'."
Write-Output ""
Write-Output "The port goes after a comma, not a colon. A colon makes the"
Write-Output "client read 127.0.0.1 as a protocol name, fall back to named"
Write-Output "pipes, and report that the network path was not found."
Write-Output ""
Write-Output ("-" * 60)
Write-Output "Ctrl+C to stop."
Write-Output ""

$serverArgs = @('-m', 'pysqlbridge.server', '--host', $Bind, '--port', "$Port")
if (-not $NoData) { $serverArgs += @('--config', $configPath) }
if ($DebugLog)    { $serverArgs += '--debug' }

Push-Location $repo
try {
    & python @serverArgs
} finally {
    Pop-Location
}

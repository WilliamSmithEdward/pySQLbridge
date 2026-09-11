# Compare whole batches with SQL Server's, by the order of what comes back.
#
# differential.ps1 sends one statement and reads one answer. A batch is a
# different thing: several statements, some of which fail, and what a client
# reads is a stream of results and errors in the order they happened. That
# order is the thing compared here, because it is where this drifts:
# whether the batch carried on past an error, whether what ran before a
# failure was kept, and whether the error arrived among the answers.
#
#   python scripts/differential.py
#   python -m pysqlbridge --config scripts/differential/config.json --port 1371
#   pwsh scripts/batches.ps1 -Port 1371
#
# FireInfoMessageEventOnUserErrors is what makes it readable: it delivers an
# error of severity 16 or less as an event instead of an exception, so the
# reader walks the whole stream rather than stopping at the first error, and
# the events arrive in the order the server sent them.
#
# Two PowerShell traps, both hit while writing this. Sequence is a reserved
# word left over from workflows, so the walker cannot be called that. And
# variable names ignore case, so $real = f $Real assigns to the parameter it
# is reading; the names here do not collide.

param(
    [int]$Port = 1371,
    [string]$RealServer = "127.0.0.1,1433",
    [string]$Fixture = ""
)

$ErrorActionPreference = "Continue"
if (-not $Fixture) {
    $Fixture = Join-Path $PSScriptRoot "differential"
}

$global:seen = New-Object System.Collections.ArrayList
$handler = {
    param($sender, $e)
    foreach ($err in $e.Errors) {
        # Class 10 and below is a PRINT or a notice, not a failure.
        if ($err.Class -gt 10) { [void]$global:seen.Add("E:" + $err.Number) }
    }
}

# Pooling off, so this session and its temporary tables end when the script
# does rather than lingering in tempdb for the next harness to read; see
# differential.ps1.
$realConn = New-Object System.Data.SqlClient.SqlConnection(
    "Server=$RealServer;Database=tempdb;Integrated Security=SSPI;Encrypt=False;TrustServerCertificate=True;Connect Timeout=10;Pooling=False")
$realConn.FireInfoMessageEventOnUserErrors = $true
$realConn.add_InfoMessage($handler)
$realConn.Open()

$setup = Get-Content (Join-Path $Fixture "setup.sql") -Raw
$cmd = $realConn.CreateCommand(); $cmd.CommandText = $setup
$cmd.ExecuteNonQuery() | Out-Null

$mineConn = New-Object System.Data.SqlClient.SqlConnection(
    "Server=127.0.0.1,$Port;Database=pysqlbridge;Integrated Security=SSPI;Encrypt=False;TrustServerCertificate=True;Connect Timeout=10;Pooling=False")
$mineConn.FireInfoMessageEventOnUserErrors = $true
$mineConn.add_InfoMessage($handler)
$mineConn.Open()

function Get-Walk($link, $sql) {
    $global:seen.Clear()
    $command = $link.CreateCommand()
    $command.CommandText = $sql
    $command.CommandTimeout = 30
    try {
        $reader = $command.ExecuteReader()
        do {
            while ($reader.Read()) {
                if ($reader.FieldCount -gt 0) {
                    $value = $reader.GetValue(0)
                    if ($value -eq [System.DBNull]::Value) {
                        [void]$global:seen.Add("R:<null>")
                    } else {
                        [void]$global:seen.Add("R:" + [string]$value)
                    }
                }
            }
        } while ($reader.NextResult())
        $reader.Close()
    } catch {
        # Severity above 16 still throws, and so does a broken connection.
        [void]$global:seen.Add(
            "X:" + $_.Exception.Message.Split([Environment]::NewLine)[0])
    }
    return ($global:seen.ToArray() -join " ")
}

$batches = Get-Content (Join-Path $Fixture "batches.json") -Raw | ConvertFrom-Json
$same = 0
$differ = 0
$gaps = 0
$closed = 0

foreach ($entry in $batches) {
    $label = $entry[0]
    $sql = $entry[1]
    # The fixture is temp tables on the real server and named tables here,
    # which is the only difference between the two statements.
    $onReal = $sql -replace '\bpeople\b', '#people' -replace '\btasks\b', '#tasks' -replace '\bwide\b', '#wide' -replace '\bmoments\b', '#moments'

    # A batch that left a transaction open would change the one after it,
    # and so would one that left XACT_ABORT on: the setting belongs to the
    # connection, and a batch it aborted never reached a SET at the end of
    # itself to put it back.
    $reset = "IF @@TRANCOUNT > 0 ROLLBACK; SET XACT_ABORT OFF"
    [void](Get-Walk $realConn $reset)
    $fromReal = Get-Walk $realConn $onReal
    [void](Get-Walk $mineConn $reset)
    $fromMine = Get-Walk $mineConn $sql

    # A batch named gap- is one this is known not to answer the way a real
    # server does, for a reason written down beside it in differential.py.
    # Listed rather than counted, so that a divergence nobody decided on
    # cannot hide among the ones somebody did.
    $known = $label.StartsWith("gap-")

    if ($fromReal -eq $fromMine) {
        if ($known) {
            # It answers the same now. Whatever was missing is here, and
            # the name should lose its prefix so a later change cannot
            # take it away again unnoticed.
            $closed++
            Write-Output ("CLOSED  " + $label + "  drop the gap- from its name")
            Write-Output ("          both: " + $fromReal)
            continue
        }
        $same++
        continue
    }
    if ($known) {
        $gaps++
        Write-Output ("GAP     " + $label)
        Write-Output ("          real: " + $fromReal)
        Write-Output ("          mine: " + $fromMine)
        continue
    }
    $differ++
    Write-Output ("DIFFER  " + $label)
    Write-Output ("          real: " + $fromReal)
    Write-Output ("          mine: " + $fromMine)
}

Write-Output ""
Write-Output "$same batches identical, $differ different"
Write-Output "$gaps known to differ, $closed of those no longer differing"
$realConn.Close()
$mineConn.Close()
if ($differ -gt 0 -or $closed -gt 0) { exit 1 }

# The truncation sweep: every battery query cut short at each word, sent to
# both servers, and what each said compared.
#
#   python scripts/differential.py
#   python -m pysqlbridge --config scripts/differential/config.json --port 1371
#   pwsh scripts/truncations.ps1 -Port 1371
#
# A query cut short is text a real server refuses, nearly always as a syntax
# error, so this compares refusals where differential.ps1 compares answers.
# Four outcomes are reported. Both refused with different numbers is the gap
# this was built to find. The bridge answering a prefix a real server
# refuses is worse: a truncated query given an answer, which nothing about
# the answer admits. A real server answering a prefix the bridge refuses is
# usually a construct not supported here, counted and sampled. Both
# answering is free differential coverage, compared the same way
# differential.ps1 compares it, with the rows sorted because a prefix has
# usually lost the ORDER BY its query ended with.
#
# One connection to each server carries every prefix, so each is followed by
# a reset on both: any transaction rolled back, XACT_ABORT off, and every
# temporary table the prefix names dropped. Without it a table one prefix
# made on one side only was still there for the next, which then refused
# with 2714 on that side for a reason the prefix had nothing to do with.
#
# Names avoid the PowerShell traps batches.ps1 records: no Sequence, and no
# variable differing from a parameter only by case.

param(
    [int]$Port = 1371,
    [string]$RealServer = "127.0.0.1,1433",
    [string]$Fixture = "",
    [int]$Limit = 0
)

$ErrorActionPreference = "Continue"
if (-not $Fixture) { $Fixture = Join-Path $PSScriptRoot "differential" }
$fixtureTables = @("#people", "#tasks", "#wide", "#moments")

function Kind-Of($name) {
    switch -Regex ($name) {
        '^(int|bigint|smallint|tinyint)$'            { return 'integer' }
        '^(float|real|decimal|numeric|money|smallmoney)$' { return 'float' }
        '^(nvarchar|varchar|nchar|char|ntext|text|sysname)$' { return 'text' }
        '^(bit)$'                                    { return 'bit' }
        '^(uniqueidentifier)$'                       { return 'guid' }
        '^(datetime|datetime2|smalldatetime|date|time)$' { return 'datetime' }
        '^(binary|varbinary|image)$'                 { return 'binary' }
        default                                      { return $name }
    }
}

function Read-Result($connection, $sql) {
    $reader = $null
    try {
        $cmd = $connection.CreateCommand()
        $cmd.CommandText = $sql
        $cmd.CommandTimeout = 30
        $reader = $cmd.ExecuteReader()
        $kinds = @()
        for ($i = 0; $i -lt $reader.FieldCount; $i++) {
            $kinds += (Kind-Of $reader.GetDataTypeName($i))
        }
        $lines = @()
        while ($reader.Read()) {
            $values = @()
            for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                $v = $reader.GetValue($i)
                if ($v -eq [System.DBNull]::Value) { $values += "<null>" }
                elseif ($v -is [double] -or $v -is [single] -or $v -is [decimal]) {
                    $values += ([math]::Round([double]$v, 6)).ToString(
                        [System.Globalization.CultureInfo]::InvariantCulture)
                }
                else { $values += [string]$v }
            }
            $lines += ($values -join " | ")
        }
        $reader.Close()
        return @{ ok = $true; rows = $lines; kinds = $kinds }
    } catch {
        if ($null -ne $reader) { try { $reader.Close() } catch { } }
        $inner = $_.Exception
        while ($null -ne $inner -and -not ($inner -is [System.Data.SqlClient.SqlException])) {
            $inner = $inner.InnerException
        }
        $number = if ($null -eq $inner) { 0 } else { $inner.Number }
        return @{
            ok = $false
            error = $_.Exception.Message.Split([Environment]::NewLine)[0]
            number = $number
        }
    }
}

function Invoke-Quiet($connection, $sql) {
    $cmd = $connection.CreateCommand(); $cmd.CommandText = $sql
    try { $cmd.ExecuteNonQuery() | Out-Null } catch { }
}

function Reset-After($connection, $sql) {
    Invoke-Quiet $connection "IF @@TRANCOUNT > 0 ROLLBACK; SET XACT_ABORT OFF"
    $made = [regex]::Matches($sql, '#\w+') | ForEach-Object { $_.Value.ToLower() } |
        Sort-Object -Unique
    foreach ($name in $made) {
        if ($fixtureTables -notcontains $name) { Invoke-Quiet $connection "DROP TABLE $name" }
    }
}

# What a message says with its particulars taken out, so that forty prefixes
# refused for the same reason count as one kind rather than forty.
function Pattern-Of($text) {
    # SqlClient wraps the server's words in its own sentence and a pair of
    # double quotes. Both come off first; stripping double-quoted runs as a
    # particular, the first version of this, took the whole message with it
    # and every kind came out as the same "_".
    $t = $text -replace '^Exception calling "ExecuteReader" with "0" argument\(s\): "', ''
    $t = $t -replace '"$', ''
    $t = $t -replace "'[^']*'", "'_'"
    $t = $t -replace '\b\d+\b', 'N'
    if ($t.Length -gt 90) { $t = $t.Substring(0, 90) }
    return $t
}

# Pooling off, so this session and its temporary tables end when the script
# does rather than lingering in tempdb for the next harness to read; see
# differential.ps1.
$realLink = New-Object System.Data.SqlClient.SqlConnection(
    "Server=$RealServer;Database=tempdb;Integrated Security=SSPI;Encrypt=False;TrustServerCertificate=True;Pooling=False")
$realLink.Open()
$setup = Get-Content (Join-Path $Fixture "setup.sql") -Raw
$cmd = $realLink.CreateCommand(); $cmd.CommandText = $setup; $cmd.ExecuteNonQuery() | Out-Null

$mineLink = New-Object System.Data.SqlClient.SqlConnection(
    "Server=127.0.0.1,$Port;Database=pysqlbridge;Integrated Security=SSPI;Encrypt=False;TrustServerCertificate=True;Pooling=False")
$mineLink.Open()

$all = Get-Content (Join-Path $Fixture "prefixes.json") -Raw | ConvertFrom-Json
if ($Limit -gt 0) { $all = $all | Select-Object -First $Limit }

$agree = 0; $bothAnswered = 0
$numbered = @{}; $numberedExample = @{}
$garbage = New-Object System.Collections.ArrayList
$onlyReal = @{}; $onlyRealExample = @{}
$rowDiffs = New-Object System.Collections.ArrayList
$started = Get-Date

foreach ($entry in $all) {
    $label = $entry[0]
    $sql = $entry[1]
    $onReal = $sql -replace '\bpeople\b', '#people' -replace '\btasks\b', '#tasks' -replace '\bwide\b', '#wide' -replace '\bmoments\b', '#moments'

    $a = Read-Result $realLink $onReal
    $b = Read-Result $mineLink $sql
    Reset-After $realLink $onReal
    Reset-After $mineLink $sql

    if (-not $a.ok -and -not $b.ok) {
        if ($a.number -eq $b.number) { $agree++; continue }
        $key = "real " + $a.number + " / mine " + $b.number + " / " + (Pattern-Of $b.error)
        if ($numbered.ContainsKey($key)) { $numbered[$key]++ }
        else {
            $numbered[$key] = 1
            $numberedExample[$key] = $sql + "   [real: " + (Pattern-Of $a.error) + "]"
        }
        continue
    }
    if (-not $a.ok -and $b.ok) {
        [void]$garbage.Add($label + "  ::  " + $sql + "   [real " + $a.number + ": " + $a.error + "]")
        continue
    }
    if ($a.ok -and -not $b.ok) {
        $key = "mine " + $b.number + " / " + (Pattern-Of $b.error)
        if ($onlyReal.ContainsKey($key)) { $onlyReal[$key]++ }
        else { $onlyReal[$key] = 1; $onlyRealExample[$key] = $sql }
        continue
    }
    $bothAnswered++
    $leftKinds = ($a.kinds -join ","); $rightKinds = ($b.kinds -join ",")
    $left = (($a.rows | Sort-Object) -join " ;; ")
    $right = (($b.rows | Sort-Object) -join " ;; ")
    if ($left -ne $right -or $leftKinds -ne $rightKinds) {
        [void]$rowDiffs.Add($label + "  ::  " + $sql)
    }
}

$took = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
Write-Output ("{0} prefixes in {1}s" -f $all.Count, $took)
Write-Output ""
Write-Output ("both refused, same number     : " + $agree)
Write-Output ("both refused, numbers differ  : " + (($numbered.Values | Measure-Object -Sum).Sum))
Write-Output ("mine answered, real refused   : " + $garbage.Count)
Write-Output ("real answered, mine refused   : " + (($onlyReal.Values | Measure-Object -Sum).Sum))
Write-Output ("both answered                 : " + $bothAnswered + "  (" + $rowDiffs.Count + " differ)")
Write-Output ""
Write-Output "=== numbers differ, by kind ==="
foreach ($key in ($numbered.Keys | Sort-Object { -$numbered[$_] })) {
    Write-Output ("{0,5}  {1}" -f $numbered[$key], $key)
    Write-Output ("         e.g. " + $numberedExample[$key])
}
Write-Output ""
Write-Output "=== mine answered what a real server refused ==="
foreach ($line in $garbage) { Write-Output ("  " + $line) }
Write-Output ""
Write-Output "=== real answered, mine refused, by kind ==="
foreach ($key in ($onlyReal.Keys | Sort-Object { -$onlyReal[$_] })) {
    Write-Output ("{0,5}  {1}" -f $onlyReal[$key], $key)
    Write-Output ("         e.g. " + $onlyRealExample[$key])
}
Write-Output ""
Write-Output "=== both answered, differently ==="
foreach ($line in $rowDiffs) { Write-Output ("  " + $line) }

$realLink.Close()
$mineLink.Close()

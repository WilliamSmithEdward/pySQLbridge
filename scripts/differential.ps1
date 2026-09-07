# Compare this server's answers with SQL Server's, query by query.
#
# Both are reached with the same client library, so a difference in the output
# is a difference in the answer rather than in how it was read. The fixture
# lives in temporary tables on the connection this opens, so nothing is left
# behind on the real server.
#
#   python scripts/differential.py
#   python -m pysqlbridge --config scripts/differential/config.json --port 1371
#   pwsh scripts/differential.ps1 -Port 1371

param(
    [int]$Port = 1371,
    [string]$RealServer = "127.0.0.1,1433",
    [string]$Fixture = ""
)

$ErrorActionPreference = "Continue"
if (-not $Fixture) {
    $Fixture = Join-Path $PSScriptRoot "differential"
}

# What a declared type is compared as. Width is left out on purpose: a source
# without a schema is sized to the values it holds, so nvarchar(5) here against
# nvarchar(20) there is the same decision made with less information. varchar
# is folded in with nvarchar for the same reason: this serves one text type on
# the wire, and every varchar value fits in it.
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
                    # Rounded so that a float and a decimal holding the same
                    # number compare equal; a real difference is larger.
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
        # An error partway through leaves the reader open, and every command
        # after it on the same connection then fails for the wrong reason.
        if ($null -ne $reader) { try { $reader.Close() } catch { } }
        return @{ ok = $false; error = $_.Exception.Message.Split([Environment]::NewLine)[0] }
    }
}

$real = New-Object System.Data.SqlClient.SqlConnection(
    "Server=$RealServer;Database=tempdb;Integrated Security=SSPI;Encrypt=False;TrustServerCertificate=True")
$real.Open()
$setup = Get-Content (Join-Path $Fixture "setup.sql") -Raw
$cmd = $real.CreateCommand(); $cmd.CommandText = $setup; $cmd.ExecuteNonQuery() | Out-Null

$mine = New-Object System.Data.SqlClient.SqlConnection(
    "Server=127.0.0.1,$Port;Database=pysqlbridge;Integrated Security=SSPI;Encrypt=False;TrustServerCertificate=True")
$mine.Open()

$queries = Get-Content (Join-Path $Fixture "queries.json") -Raw | ConvertFrom-Json
$same = 0; $differ = 0; $refused = 0; $mistyped = 0

foreach ($entry in $queries) {
    $label = $entry[0]
    $sql = $entry[1]
    # The fixture is in temp tables on the real server and in named tables
    # here, which is the only difference between the two statements.
    $onReal = $sql -replace '\bpeople\b', '#people' -replace '\btasks\b', '#tasks'

    $a = Read-Result $real $onReal
    $b = Read-Result $mine $sql

    if (-not $a.ok -and -not $b.ok) { $same++; continue }
    if (-not $a.ok) {
        $differ++
        Write-Output ("ONLY-MINE " + $label.PadRight(20) + "real refused: " + $a.error)
        Write-Output ("            mine: " + (($b.rows | Select-Object -First 2) -join " ;; "))
        continue
    }
    if (-not $b.ok) {
        $refused++
        Write-Output ("REFUSED   " + $label.PadRight(20) + $b.error)
        Write-Output ("            real: " + (($a.rows | Select-Object -First 3) -join " ;; "))
        continue
    }
    $leftKinds = ($a.kinds -join ",")
    $rightKinds = ($b.kinds -join ",")
    if ($leftKinds -ne $rightKinds) {
        $mistyped++
        Write-Output ("TYPE      " + $label.PadRight(20) + $sql.Substring(0, [Math]::Min(58, $sql.Length)))
        Write-Output ("            real: " + $leftKinds)
        Write-Output ("            mine: " + $rightKinds)
    }

    $left = ($a.rows -join " ;; ")
    $right = ($b.rows -join " ;; ")
    if ($left -eq $right) { $same++; continue }
    $differ++
    Write-Output ("DIFF      " + $label.PadRight(20) + $sql.Substring(0, [Math]::Min(58, $sql.Length)))
    Write-Output ("            real: " + $left.Substring(0, [Math]::Min(90, $left.Length)))
    Write-Output ("            mine: " + $right.Substring(0, [Math]::Min(90, $right.Length)))
}

Write-Output ""
Write-Output "$same identical, $differ different, $refused refused by pysqlbridge"
Write-Output "$mistyped of them declared a different kind of column"
$real.Close()
$mine.Close()
if ($differ -gt 0 -or $refused -gt 0 -or $mistyped -gt 0) { exit 1 }

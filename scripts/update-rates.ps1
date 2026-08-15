<#
.SYNOPSIS
  Refreshes the USD exchange rates the Token-Usage dashboard displays.

.DESCRIPTION
  Fetches current rates and writes them to a small JSON file that the container
  reads through a read-only bind mount. Running the lookup here, on the host,
  keeps the container itself free of outbound network calls.

  The dashboard re-reads the file within a few minutes, so no rebuild or
  restart is needed.

  Intended to run weekly via Task Scheduler; safe to run by hand any time.

.PARAMETER OutFile
  Where to write rates.json. Defaults to the directory bind-mounted into the
  container. Deliberately outside the git repo so weekly updates don't show up
  as uncommitted changes.
#>
[CmdletBinding()]
param(
    [string]$OutFile = "$env:USERPROFILE\.token-usage\rates.json",
    [string[]]$Currencies = @("AUD", "GBP")
)

$ErrorActionPreference = "Stop"

$dir = Split-Path -Parent $OutFile
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

# No API key required, USD base.
$endpoint = "https://open.er-api.com/v6/latest/USD"

try {
    $resp = Invoke-RestMethod -Uri $endpoint -TimeoutSec 30
} catch {
    Write-Error "Rate lookup failed: $($_.Exception.Message). Leaving the existing rates file untouched."
    exit 1
}

if ($resp.result -ne "success") {
    Write-Error "Rate provider returned result='$($resp.result)'. Leaving the existing rates file untouched."
    exit 1
}

$rates = @{}
foreach ($code in $Currencies) {
    $value = $resp.rates.$code
    # Guard against a malformed or partial response overwriting good data.
    if ($null -eq $value -or $value -le 0) {
        Write-Error "Provider did not return a usable rate for $code. Leaving the existing rates file untouched."
        exit 1
    }
    $rates[$code] = [math]::Round([double]$value, 4)
}

$doc = [ordered]@{
    base       = "USD"
    source     = "open.er-api.com"
    updated_at = (Get-Date).ToString("o")
    rates      = $rates
}

# Written without a BOM: Out-File -Encoding utf8 emits one on PowerShell 5.1,
# and a leading BOM breaks strict JSON parsers.
$json = $doc | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding $false))

$summary = ($rates.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "  "
Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm')  wrote $OutFile  ($summary)"

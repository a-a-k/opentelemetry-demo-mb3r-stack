Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-RepoRoot {
  return (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}

function Get-ComposeArgs {
  $root = Get-RepoRoot
  return @(
    '--env-file', (Join-Path $root '.env'),
    '--env-file', (Join-Path $root '.env.override'),
    '-f', (Join-Path $root 'docker-compose.yml'),
    '-f', (Join-Path $root 'mb3r\docker-compose.mb3r.yml')
  )
}

function Invoke-JsonRequest {
  param(
    [Parameter(Mandatory = $true)][string]$Uri,
    [string]$Method = 'GET',
    [object]$Body,
    [int]$TimeoutSec = 5
  )

  if ($PSBoundParameters.ContainsKey('Body')) {
    return Invoke-RestMethod -Uri $Uri -Method $Method -ContentType 'application/json' -Body ($Body | ConvertTo-Json -Depth 8) -TimeoutSec $TimeoutSec
  }

  return Invoke-RestMethod -Uri $Uri -Method $Method -TimeoutSec $TimeoutSec
}

function Wait-Until {
  param(
    [Parameter(Mandatory = $true)][scriptblock]$Condition,
    [int]$TimeoutSeconds = 180,
    [int]$DelaySeconds = 2,
    [string]$Message = 'condition'
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $result = & $Condition
      if ($result) {
        return $result
      }
    } catch {
    }
    Start-Sleep -Seconds $DelaySeconds
  }

  throw "Timed out waiting for $Message after $TimeoutSeconds seconds."
}

function Get-PrometheusQueryResult {
  param(
    [Parameter(Mandatory = $true)][string]$Query,
    [string]$PrometheusBaseUrl = 'http://localhost:9090'
  )

  $encoded = [System.Uri]::EscapeDataString($Query)
  $response = Invoke-JsonRequest -Uri "$PrometheusBaseUrl/api/v1/query?query=$encoded"
  if ($response.status -ne 'success') {
    throw "Prometheus query failed for: $Query"
  }
  return @($response.data.result)
}

function Get-ScalarResult {
  param(
    [Parameter(Mandatory = $true)][string]$Query,
    [string]$PrometheusBaseUrl = 'http://localhost:9090'
  )

  $result = Get-PrometheusQueryResult -Query $Query -PrometheusBaseUrl $PrometheusBaseUrl
  $result = @($result)
  if (-not $result -or $result.Count -eq 0) {
    return $null
  }
  return [double]$result[0].value[1]
}

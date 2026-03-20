param(
  [int]$TimeoutSeconds = 420,
  [string]$BaseUrl = 'http://localhost:8080'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'common.ps1')

$repoRoot = Get-RepoRoot
$artifactPath = Join-Path $repoRoot 'mb3r\out\artifacts\latest-snapshot.json'
$reportPath = Join-Path $repoRoot 'mb3r\out\reports\current-report.json'
$dashboardQueryUrl = 'http://localhost:8080/grafana/api/search?query=MB3R%20Resilience%20Posture'
$requiredEndpoints = @(
  'frontend:GET /api/products',
  'frontend:GET /api/recommendations',
  'frontend:GET /api/cart',
  'frontend:POST /api/checkout'
)

Write-Host 'Smoke check: waiting for the Astronomy Shop frontend.'
Wait-Until -Message 'frontend response' -TimeoutSeconds $TimeoutSeconds -Condition {
  $response = Invoke-WebRequest -Uri $BaseUrl -UseBasicParsing -TimeoutSec 5
  return $response.StatusCode -eq 200
} | Out-Null

Write-Host 'Smoke check: waiting for Bering readiness.'
Wait-Until -Message 'Bering readiness' -TimeoutSeconds $TimeoutSeconds -Condition {
  $ready = Invoke-WebRequest -Uri 'http://localhost:14318/readyz' -UseBasicParsing -TimeoutSec 5
  return $ready.StatusCode -eq 200
} | Out-Null

Write-Host 'Smoke check: generating deterministic traffic.'
Wait-Until -Message 'deterministic frontend traffic' -TimeoutSeconds $TimeoutSeconds -Condition {
  & (Join-Path $PSScriptRoot 'traffic.ps1') -Scenario all -Iterations 5 -DelaySeconds 1 -BaseUrl $BaseUrl
  return $true
} | Out-Null

Write-Host 'Smoke check: waiting for the latest Bering artifact.'
$artifact = Wait-Until -Message 'Bering latest artifact' -TimeoutSeconds $TimeoutSeconds -Condition {
  if (-not (Test-Path $artifactPath)) {
    return $false
  }
  $snapshot = Get-Content $artifactPath | ConvertFrom-Json
  if ($snapshot.metadata.schema.name -eq 'io.mb3r.bering.snapshot' -and $snapshot.ingest.spans -gt 0) {
    return $snapshot
  }
  return $false
}

Write-Host 'Smoke check: waiting for Sheaft readiness and the current report.'
Wait-Until -Message 'Sheaft readiness' -TimeoutSeconds $TimeoutSeconds -Condition {
  $ready = Invoke-JsonRequest -Uri 'http://localhost:19080/readyz'
  return $ready.ready -eq $true
} | Out-Null

$report = Wait-Until -Message 'Sheaft current report' -TimeoutSeconds $TimeoutSeconds -Condition {
  $current = Invoke-JsonRequest -Uri 'http://localhost:19080/current-report'
  if (-not $current.generated_at -or -not $current.policy_evaluation.decision) {
    return $false
  }

  $endpointIds = @($current.endpoint_results | ForEach-Object { [string]$_.endpoint_id })
  $missingEndpoints = @($requiredEndpoints | Where-Object { $_ -notin $endpointIds })
  if ($missingEndpoints.Count -eq 0) {
    return $current
  }
  return $false
}

Write-Host 'Smoke check: waiting for Prometheus to see MB3R posture metrics.'
Wait-Until -Message 'Prometheus MB3R metrics' -TimeoutSeconds $TimeoutSeconds -Condition {
  $score = Get-ScalarResult -Query 'mb3r_posture_score'
  if ($null -eq $score) {
    return $false
  }
  return [double]$score -ge 0.75
} | Out-Null

Write-Host 'Smoke check: waiting for Grafana dashboard provisioning.'
Wait-Until -Message 'Grafana MB3R dashboard' -TimeoutSeconds $TimeoutSeconds -Condition {
  $dashboards = @(
    Invoke-JsonRequest -Uri $dashboardQueryUrl
  )
  return (@($dashboards | Where-Object { $_.uid -eq 'mb3r-resilience' })).Count -gt 0
} | Out-Null

$savedReport = if (Test-Path $reportPath) {
  Get-Content $reportPath | ConvertFrom-Json
} else {
  $report
}

$score = Get-ScalarResult -Query 'mb3r_posture_score'
if ($null -eq $score) {
  if ($savedReport.summary.weighted_overall_availability) {
    $score = [double]$savedReport.summary.weighted_overall_availability
  } else {
    $score = [double]$savedReport.summary.overall_availability
  }
}

Write-Host ''
Write-Host 'MB3R smoke check passed.'
Write-Host "Bering snapshot id: $($artifact.snapshot_id)"
Write-Host "Bering trace count:  $($artifact.ingest.traces)"
Write-Host "Sheaft decision:     $($savedReport.policy_evaluation.decision)"
Write-Host ("Posture score:       {0:N4}" -f $score)
Write-Host 'Grafana dashboard:   http://localhost:8080/grafana/d/mb3r-resilience/mb3r-resilience-posture'

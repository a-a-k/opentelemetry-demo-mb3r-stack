param(
  [ValidateSet('fail', 'restore')]
  [string]$Action = 'fail',
  [string]$Service = 'recommendation',
  [ValidateSet('all', 'core', 'products', 'recommendations', 'cart', 'checkout')]
  [string]$Scenario = 'core',
  [int]$TimeoutSeconds = 240
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'common.ps1')

$repoRoot = Get-RepoRoot
$composeArgs = Get-ComposeArgs
$baselinePath = Join-Path $repoRoot 'mb3r\out\baseline\healthy-report.json'
$baselineMetricsPath = Join-Path $repoRoot 'mb3r\out\baseline\healthy-metrics.json'
$primaryProfile = 'steady-state'
$scenarioEndpoints = @{
  all             = @(
    'frontend:GET /api/products',
    'frontend:GET /api/recommendations',
    'frontend:GET /api/cart',
    'frontend:POST /api/checkout'
  )
  core            = @(
    'frontend:GET /api/products',
    'frontend:GET /api/cart',
    'frontend:POST /api/checkout'
  )
  products        = @('frontend:GET /api/products')
  recommendations = @('frontend:GET /api/recommendations')
  cart            = @('frontend:GET /api/cart')
  checkout        = @('frontend:POST /api/checkout')
}

function Get-CurrentReport {
  return Invoke-JsonRequest -Uri 'http://localhost:19080/current-report'
}

function Reset-BeringWindow {
  & docker compose @composeArgs restart bering | Out-Host
  Wait-Until -Message 'Bering readiness after reset' -TimeoutSeconds $TimeoutSeconds -Condition {
    try {
      $ready = Invoke-WebRequest -Uri 'http://localhost:14318/readyz' -UseBasicParsing -TimeoutSec 5
      return $ready.StatusCode -eq 200
    } catch {
      return $false
    }
  } | Out-Null
}

function Get-CurrentDecision {
  $report = Get-CurrentReport
  if ($report.policy_evaluation.decision) {
    return [string]$report.policy_evaluation.decision
  }

  try {
    $status = Invoke-JsonRequest -Uri 'http://localhost:19080/status'
    if ($status.decision) {
      return [string]$status.decision
    }
  } catch {
  }

  return 'error'
}

function Get-ReportGeneratedAt {
  param(
    [Parameter(Mandatory = $true)][object]$Report
  )

  if (-not $Report.generated_at) {
    return $null
  }

  return ([datetimeoffset]::Parse([string]$Report.generated_at)).UtcDateTime
}

function Wait-ForScenarioReport {
  param(
    [int]$TimeoutSeconds,
    [string]$Message,
    [datetime]$NotBefore = [datetime]::MinValue
  )

  $requiredEndpoints = @($scenarioEndpoints[$Scenario])
  $notBeforeUtc = $NotBefore.ToUniversalTime()

  return Wait-Until -Message $Message -TimeoutSeconds $TimeoutSeconds -Condition {
    $report = Get-CurrentReport
    $generatedAt = Get-ReportGeneratedAt -Report $report
    $endpointIds = @($report.endpoint_results | ForEach-Object { [string]$_.endpoint_id })
    $missingEndpoints = @($requiredEndpoints | Where-Object { $_ -notin $endpointIds })
    if ($generatedAt -and $generatedAt -gt $notBeforeUtc -and $missingEndpoints.Count -eq 0) {
      return $report
    }
    return $false
  }
}

function Wait-ForSuccessfulTraffic {
  param(
    [string]$Message,
    [int]$Iterations = 8,
    [string]$TrafficScenario = $Scenario
  )

  Wait-Until -Message $Message -TimeoutSeconds $TimeoutSeconds -Condition {
    & (Join-Path $PSScriptRoot 'traffic.ps1') -Scenario $TrafficScenario -Iterations $Iterations -DelaySeconds 1
    return $true
  } | Out-Null
}

function Invoke-FailureTraffic {
  param(
    [int]$Iterations = 2
  )

  & (Join-Path $PSScriptRoot 'traffic.ps1') -Scenario $Scenario -Iterations $Iterations -DelaySeconds 0 -IgnoreErrors
}

function Get-ExporterPostureScore {
  return Get-ScalarResult -Query 'mb3r_posture_score'
}

function Get-EscapedPrometheusLabel {
  param(
    [Parameter(Mandatory = $true)][string]$Value
  )

  return $Value.Replace('\', '\\').Replace('"', '\"')
}

function Get-ExporterEndpointAvailability {
  param(
    [Parameter(Mandatory = $true)][string]$EndpointId
  )

  $escapedEndpoint = Get-EscapedPrometheusLabel -Value $EndpointId
  return Get-ScalarResult -Query "avg by () (mb3r_endpoint_availability{profile=""$primaryProfile"",endpoint=""$escapedEndpoint""})"
}

function Get-ScenarioMetricScore {
  $scores = @()
  foreach ($endpointId in $scenarioEndpoints[$Scenario]) {
    $value = Get-ExporterEndpointAvailability -EndpointId $endpointId
    if ($null -eq $value) {
      return $null
    }
    $scores += [double]$value
  }

  if ($scores.Count -eq 0) {
    return $null
  }

  return ($scores | Measure-Object -Average).Average
}

function Write-BaselineMetrics {
  param(
    [string]$Decision,
    [double]$PostureScore,
    [double]$ScenarioScore
  )

  $payload = [ordered]@{
    generated_at   = (Get-Date).ToUniversalTime().ToString('o')
    service        = $Service
    scenario       = $Scenario
    decision       = $Decision
    posture_score  = $PostureScore
    scenario_score = $ScenarioScore
  }

  $payload | ConvertTo-Json -Depth 5 | Set-Content -Path $baselineMetricsPath
}

function Read-BaselineMetrics {
  if (-not (Test-Path $baselineMetricsPath)) {
    return $null
  }

  return Get-Content $baselineMetricsPath | ConvertFrom-Json
}

if ($Action -eq 'restore') {
  & docker compose @composeArgs start $Service | Out-Host
  Reset-BeringWindow
  & (Join-Path $PSScriptRoot 'smoke.ps1') -TimeoutSeconds $TimeoutSeconds
  $restored = @{
    decision = Get-CurrentDecision
    posture = Get-ExporterPostureScore
    scenario = Get-ScenarioMetricScore
  }

  Write-Host "Restored service '$Service'."
  Write-Host "Decision: $($restored.decision)"
  Write-Host ("Posture score: {0:N4}" -f [double]$restored.posture)
  Write-Host ("Scenario score: {0:N4}" -f [double]$restored.scenario)
  exit 0
}

$healthyStart = (Get-Date).ToUniversalTime()
Wait-ForSuccessfulTraffic -Message 'healthy scenario traffic' -TrafficScenario 'all'
$healthyReport = Wait-ForScenarioReport -TimeoutSeconds $TimeoutSeconds -Message 'healthy scenario report' -NotBefore $healthyStart
$healthyReport | ConvertTo-Json -Depth 10 | Set-Content -Path $baselinePath

$beforeDecision = Get-CurrentDecision
$beforePosture = Wait-Until -Message 'healthy MB3R posture score' -TimeoutSeconds $TimeoutSeconds -Condition {
  $score = Get-ExporterPostureScore
  if ($null -ne $score -and [double]$score -ge 0.75) {
    return [double]$score
  }
  return $false
}
$beforeScenarioScore = Wait-Until -Message 'healthy MB3R scenario score' -TimeoutSeconds $TimeoutSeconds -Condition {
  $score = Get-ScenarioMetricScore
  if ($null -ne $score -and [double]$score -ge 0.85) {
    return [double]$score
  }
  return $false
}

Write-BaselineMetrics -Decision $beforeDecision -PostureScore $beforePosture -ScenarioScore $beforeScenarioScore

Write-Host "Capturing healthy baseline to $baselinePath"
Write-Host "Stopping service '$Service' and exercising '$Scenario' traffic."
& docker compose @composeArgs stop $Service | Out-Host
Reset-BeringWindow
Invoke-FailureTraffic -Iterations 2

$changed = Wait-Until -Message 'a visible MB3R posture degradation' -TimeoutSeconds $TimeoutSeconds -Condition {
  Invoke-FailureTraffic -Iterations 1 | Out-Null
  $posture = Get-ExporterPostureScore
  $scenarioScore = Get-ScenarioMetricScore
  if ($null -eq $posture -or $null -eq $scenarioScore) {
    return $false
  }

  $decision = Get-CurrentDecision
  if ($beforeDecision -eq 'pass' -and ($decision -eq 'warn' -or $decision -eq 'fail' -or $decision -eq 'error')) {
    return @{
      decision = $decision
      posture = $posture
      scenario = $scenarioScore
    }
  }
  if (($beforeScenarioScore - $scenarioScore) -ge 0.10) {
    return @{
      decision = $decision
      posture = $posture
      scenario = $scenarioScore
    }
  }
  if (($beforePosture - $posture) -ge 0.05) {
    return @{
      decision = $decision
      posture = $posture
      scenario = $scenarioScore
    }
  }
  return $false
}

Write-Host ''
Write-Host "Fault demo complete for service '$Service'."
Write-Host "Before: decision=$beforeDecision posture=$([math]::Round([double]$beforePosture, 4)) scenario=$([math]::Round([double]$beforeScenarioScore, 4))"
Write-Host "After:  decision=$($changed.decision) posture=$([math]::Round([double]$changed.posture, 4)) scenario=$([math]::Round([double]$changed.scenario, 4))"
Write-Host "Restore with: powershell -ExecutionPolicy Bypass -File mb3r/scripts/fault-demo.ps1 -Action restore -Service $Service -Scenario $Scenario"

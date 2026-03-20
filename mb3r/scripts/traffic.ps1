param(
  [ValidateSet('all', 'core', 'products', 'recommendations', 'cart', 'checkout')]
  [string]$Scenario = 'all',
  [int]$Iterations = 3,
  [int]$DelaySeconds = 1,
  [string]$BaseUrl = 'http://localhost:8080',
  [switch]$IgnoreErrors
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'common.ps1')

$script:FailedSteps = 0

function Invoke-Step {
  param(
    [Parameter(Mandatory = $true)][scriptblock]$Action,
    [Parameter(Mandatory = $true)][string]$Description
  )

  try {
    return & $Action
  } catch {
    if (-not $IgnoreErrors) {
      throw
    }
    $script:FailedSteps++
    Write-Warning "Traffic step failed: $Description ($($_.Exception.Message))"
    return $null
  }
}

function Invoke-TrafficIteration {
  $products = Invoke-Step -Description 'GET /api/products' -Action {
    Invoke-JsonRequest -Uri "$BaseUrl/api/products?currencyCode=USD"
  }
  if (-not $products -or $products.Count -eq 0) {
    throw 'The demo did not return any products.'
  }

  $product = $products[0]
  $productId = $product.id
  if (-not $productId) {
    throw 'A product id could not be resolved from /api/products.'
  }

  $userId = [guid]::NewGuid().ToString()
  $peoplePath = Join-Path (Get-RepoRoot) 'src\load-generator\people.json'
  $person = (Get-Content $peoplePath | ConvertFrom-Json)[0]
  if ($person.PSObject.Properties.Name -contains 'userId') {
    $person.userId = $userId
  } else {
    $person | Add-Member -NotePropertyName 'userId' -NotePropertyValue $userId
  }

  switch ($Scenario) {
    'products' {
      [void](Invoke-Step -Description 'GET /api/products' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/products?currencyCode=USD"
        })
    }
    'recommendations' {
      [void](Invoke-Step -Description 'GET /api/recommendations' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/recommendations?productIds=$productId&sessionId=$userId&currencyCode=USD"
        })
    }
    'cart' {
      [void](Invoke-Step -Description 'POST /api/cart' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/cart" -Method 'POST' -Body @{
            userId = $userId
            item   = @{
              productId = $productId
              quantity  = 1
            }
          }
        })
      [void](Invoke-Step -Description 'GET /api/cart' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/cart?sessionId=$userId&currencyCode=USD"
        })
    }
    'checkout' {
      [void](Invoke-Step -Description 'POST /api/cart' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/cart" -Method 'POST' -Body @{
            userId = $userId
            item   = @{
              productId = $productId
              quantity  = 1
            }
          }
        })
      [void](Invoke-Step -Description 'POST /api/checkout' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/checkout?currencyCode=USD" -Method 'POST' -Body $person
        })
    }
    'core' {
      [void](Invoke-Step -Description 'GET /api/products' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/products?currencyCode=USD"
        })
      [void](Invoke-Step -Description 'POST /api/cart' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/cart" -Method 'POST' -Body @{
            userId = $userId
            item   = @{
              productId = $productId
              quantity  = 1
            }
          }
        })
      [void](Invoke-Step -Description 'GET /api/cart' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/cart?sessionId=$userId&currencyCode=USD"
        })
      [void](Invoke-Step -Description 'POST /api/checkout' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/checkout?currencyCode=USD" -Method 'POST' -Body $person
        })
    }
    default {
      [void](Invoke-Step -Description 'GET /api/products' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/products?currencyCode=USD"
        })
      [void](Invoke-Step -Description 'GET /api/recommendations' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/recommendations?productIds=$productId&sessionId=$userId&currencyCode=USD"
        })
      [void](Invoke-Step -Description 'POST /api/cart' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/cart" -Method 'POST' -Body @{
            userId = $userId
            item   = @{
              productId = $productId
              quantity  = 1
            }
          }
        })
      [void](Invoke-Step -Description 'GET /api/cart' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/cart?sessionId=$userId&currencyCode=USD"
        })
      [void](Invoke-Step -Description 'POST /api/checkout' -Action {
          Invoke-JsonRequest -Uri "$BaseUrl/api/checkout?currencyCode=USD" -Method 'POST' -Body $person
        })
    }
  }
}

for ($i = 0; $i -lt $Iterations; $i++) {
  Invoke-TrafficIteration
  if ($DelaySeconds -gt 0 -and $i -lt ($Iterations - 1)) {
    Start-Sleep -Seconds $DelaySeconds
  }
}

Write-Host "Generated $Iterations deterministic '$Scenario' traffic iteration(s) against $BaseUrl."
if ($IgnoreErrors) {
  Write-Host "Ignored traffic step failures: $script:FailedSteps"
}

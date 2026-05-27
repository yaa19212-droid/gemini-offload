$ErrorActionPreference = "Stop"

function Get-ConfigEnvValue {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Body,
    [Parameter(Mandatory = $true)]
    [string]$Name
  )

  $match = [regex]::Match(
    $Body,
    '(?m)^\s*' + [regex]::Escape($Name) + '\s*=\s*"(?<value>(?:\\.|[^"])*)"'
  )
  if (-not $match.Success) {
    return $null
  }

  return ($match.Groups["value"].Value -replace '\\"', '"')
}

function Import-CodexGeminiEnv {
  $configPath = Join-Path $HOME '.codex\config.toml'
  if (-not (Test-Path -LiteralPath $configPath)) {
    return
  }

  $content = Get-Content -LiteralPath $configPath -Raw
  $sectionMatch = [regex]::Match(
    $content,
    '(?ms)^\[mcp_servers\.gemini-offload\.env\]\s*(?<body>.*?)(?=^\[|\z)'
  )
  if (-not $sectionMatch.Success) {
    return
  }

  $body = $sectionMatch.Groups["body"].Value
  foreach ($name in @("GEMINI_OFFLOAD_VERTEX_CREDENTIALS", "VERTEX_AI_CREDENTIALS", "GOOGLE_CLOUD_LOCATION", "VERTEX_AI_LOCATION", "GEMINI_OFFLOAD_REPO", "GEMINI_OFFLOAD_OUTPUT_DIR")) {
    $currentValue = (Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue).Value
    if (-not [string]::IsNullOrWhiteSpace($currentValue)) {
      continue
    }

    $configValue = Get-ConfigEnvValue -Body $body -Name $name
    if (-not [string]::IsNullOrWhiteSpace($configValue)) {
      Set-Item -Path "Env:$name" -Value $configValue
    }
  }
}

Import-CodexGeminiEnv

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginRoot = Split-Path -Parent $scriptDir

$configuredRepo = (Get-Item -Path "Env:GEMINI_OFFLOAD_REPO" -ErrorAction SilentlyContinue).Value
if (-not [string]::IsNullOrWhiteSpace($configuredRepo)) {
  if (-not (Test-Path -LiteralPath $configuredRepo)) {
    throw "GEMINI_OFFLOAD_REPO points to a missing path: $configuredRepo"
  }
  $repoRoot = Resolve-Path -LiteralPath $configuredRepo
} else {
  $repoRoot = Resolve-Path (Join-Path $pluginRoot "..\\..")
}

if (-not (Test-Path -LiteralPath $repoRoot)) {
  throw "gemini-offload repo root could not be resolved. Set GEMINI_OFFLOAD_REPO to the checked-out repository."
}

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "mcp_server"))) {
  throw "gemini-offload repo root has no mcp_server package. Set GEMINI_OFFLOAD_REPO to the checked-out repository."
}

Set-Location -LiteralPath $repoRoot

if (Get-Command py -ErrorAction SilentlyContinue) {
  & py -m mcp_server
  exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
  & python -m mcp_server
  exit $LASTEXITCODE
}

throw "Python was not found on PATH. Install Python 3.10+ or add py/python to PATH."

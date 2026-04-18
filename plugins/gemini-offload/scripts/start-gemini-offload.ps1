$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginRoot = Split-Path -Parent $scriptDir
$repoRoot = Resolve-Path (Join-Path $pluginRoot "..\\..")

if (-not (Test-Path -LiteralPath $repoRoot)) {
  throw "gemini-offload repo root could not be resolved."
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

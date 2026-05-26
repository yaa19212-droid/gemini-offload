[CmdletBinding()]
param(
  [string]$ManifestPath = (Join-Path $HOME ".secrets\vertex-ai\service-accounts\manifest.json"),
  [string]$GoogleCloudLocation = "global",
  [switch]$SkipEditableInstall
)

$ErrorActionPreference = "Stop"

function Convert-ToForwardSlashPath {
  param([Parameter(Mandatory = $true)][string]$Path)
  return $Path.Replace("\", "/")
}

function Resolve-ExistingPathForConfig {
  param([Parameter(Mandatory = $true)][string]$Path)
  return Convert-ToForwardSlashPath -Path (Resolve-Path -LiteralPath $Path).Path
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "mcp_server"))) {
  throw "Run this script from a gemini-offload checkout that contains the mcp_server package."
}

if (-not $SkipEditableInstall) {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -m pip install -e $repoRoot
  } elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m pip install -e $repoRoot
  } else {
    throw "Python was not found on PATH. Install Python 3.10+ or add py/python to PATH."
  }

  if ($LASTEXITCODE -ne 0) {
    throw "Editable install failed."
  }
}

$repoPath = Resolve-ExistingPathForConfig -Path $repoRoot
$startScriptPath = Resolve-ExistingPathForConfig -Path (Join-Path $repoRoot "plugins\gemini-offload\scripts\start-gemini-offload.ps1")
$manifestConfigPath = Convert-ToForwardSlashPath -Path $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ManifestPath)

Write-Host ""
if (Test-Path -LiteralPath $ManifestPath) {
  Write-Host "Vertex credential manifest found: $manifestConfigPath"
} else {
  Write-Warning "Vertex credential manifest was not found: $manifestConfigPath"
  Write-Host "Create it there, or pass -ManifestPath with the path used on this machine."
}

Write-Host ""
Write-Host "Add or update this block in ~/.codex/config.toml:"
Write-Host ""
Write-Host "[mcp_servers.gemini-offload]"
Write-Host 'command = "powershell"'
Write-Host "args = [""-NoProfile"", ""-ExecutionPolicy"", ""Bypass"", ""-File"", ""$startScriptPath""]"
Write-Host "startup_timeout_sec = 1800"
Write-Host "tool_timeout_sec = 1800"
Write-Host ""
Write-Host "[mcp_servers.gemini-offload.env]"
Write-Host "GEMINI_OFFLOAD_REPO = ""$repoPath"""
Write-Host "GEMINI_OFFLOAD_VERTEX_CREDENTIALS = ""$manifestConfigPath"""
Write-Host "GOOGLE_CLOUD_LOCATION = ""$GoogleCloudLocation"""
Write-Host ""
Write-Host "Restart Codex or open a new session after updating config.toml."

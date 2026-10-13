[CmdletBinding(PositionalBinding = $false)]
param(
  [string]$CodexHome = "$env:USERPROFILE\.codex",
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$CodexArgs
)

$ErrorActionPreference = "Stop"

function Get-ConfiguredCliPath([string]$configPath) {
  if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return "" }
  $content = Get-Content -Raw -LiteralPath $configPath -Encoding UTF8
  foreach ($pattern in @(
    "(?m)^\s*CODEX_CLI_PATH\s*=\s*'([^']+)'",
    '(?m)^\s*CODEX_CLI_PATH\s*=\s*"([^"]+)"'
  )) {
    $match = [regex]::Match($content, $pattern)
    if ($match.Success) { return $match.Groups[1].Value }
  }
  return ""
}

$configPath = Join-Path $CodexHome "config.toml"
$configured = Get-ConfiguredCliPath $configPath
$candidates = @()
if (-not [string]::IsNullOrWhiteSpace($configured)) {
  $candidates += $configured
}

$binRoot = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin"
$candidates += @(
  Get-ChildItem -LiteralPath $binRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    ForEach-Object { Join-Path $_.FullName "codex.exe" }
)

$cliPath = $candidates |
  Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
  Select-Object -Unique |
  Select-Object -First 1

if ([string]::IsNullOrWhiteSpace([string]$cliPath)) {
  throw "No runnable LocalAppData Codex CLI was found."
}

& $cliPath @CodexArgs
exit $LASTEXITCODE

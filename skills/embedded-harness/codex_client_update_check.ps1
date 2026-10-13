[CmdletBinding()]
param(
  [string]$CodexHome = "$env:USERPROFILE\.codex",
  [string]$ConfigFilePath = "",
  [string]$AppInstallRoot = "",
  [string]$AppVersionOverride = "",
  [string]$LocalCodexRoot = ""
)

$ErrorActionPreference = "Stop"

function Test-ExistingFile([string]$Path) {
  return (-not [string]::IsNullOrWhiteSpace($Path)) -and
    (Test-Path -LiteralPath $Path -PathType Leaf)
}

function Test-ExistingDir([string]$Path) {
  return (-not [string]::IsNullOrWhiteSpace($Path)) -and
    (Test-Path -LiteralPath $Path -PathType Container)
}

function ConvertTo-NormalPath([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
  try {
    return [IO.Path]::GetFullPath($Path).TrimEnd(
      [IO.Path]::DirectorySeparatorChar,
      [IO.Path]::AltDirectorySeparatorChar
    )
  } catch {
    return $Path.TrimEnd('\', '/')
  }
}

function Test-SamePath([string]$Left, [string]$Right) {
  $normalizedLeft = ConvertTo-NormalPath $Left
  $normalizedRight = ConvertTo-NormalPath $Right
  return (-not [string]::IsNullOrWhiteSpace($normalizedLeft)) -and
    $normalizedLeft.Equals($normalizedRight, [StringComparison]::OrdinalIgnoreCase)
}

function Get-ConfigValue([string]$Content, [string]$Name) {
  $singleQuoted = "(?m)^\s*$([regex]::Escape($Name))\s*=\s*'([^']*)'"
  $match = [regex]::Match($Content, $singleQuoted)
  if ($match.Success) { return $match.Groups[1].Value }

  $doubleQuoted = "(?m)^\s*$([regex]::Escape($Name))\s*=\s*`"([^`"]*)`""
  $match = [regex]::Match($Content, $doubleQuoted)
  if ($match.Success) { return $match.Groups[1].Value }
  return ""
}

$configPath = if ([string]::IsNullOrWhiteSpace($ConfigFilePath)) {
  Join-Path $CodexHome "config.toml"
} else {
  $ConfigFilePath
}

if (-not (Test-ExistingFile $configPath)) {
  throw "config.toml not found at $configPath"
}

$content = Get-Content -Raw -LiteralPath $configPath -Encoding UTF8
$appx = if ([string]::IsNullOrWhiteSpace($AppInstallRoot) -or
  [string]::IsNullOrWhiteSpace($AppVersionOverride)) {
  Get-AppxPackage OpenAI.Codex -ErrorAction SilentlyContinue |
    Sort-Object Version -Descending |
    Select-Object -First 1
} else {
  $null
}

$appVersion = if (-not [string]::IsNullOrWhiteSpace($AppVersionOverride)) {
  $AppVersionOverride
} elseif ($appx) {
  [string]$appx.Version
} else {
  ""
}

$effectiveAppInstallRoot = if (-not [string]::IsNullOrWhiteSpace($AppInstallRoot)) {
  $AppInstallRoot
} elseif ($appx) {
  [string]$appx.InstallLocation
} else {
  ""
}

$expectedVersionMarker = ""
$versionMarkerSource = "unavailable"
if (Test-ExistingDir $effectiveAppInstallRoot) {
  foreach ($pluginName in @("browser", "chrome")) {
    $manifestPath = Join-Path $effectiveAppInstallRoot "app\resources\plugins\openai-bundled\plugins\$pluginName\.codex-plugin\plugin.json"
    if (-not (Test-ExistingFile $manifestPath)) { continue }
    try {
      $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 | ConvertFrom-Json
      if (-not [string]::IsNullOrWhiteSpace([string]$manifest.version)) {
        $expectedVersionMarker = [string]$manifest.version
        $versionMarkerSource = "bundled_${pluginName}_plugin_manifest"
        break
      }
    } catch {
      continue
    }
  }
}
if ([string]::IsNullOrWhiteSpace($expectedVersionMarker) -and
  -not [string]::IsNullOrWhiteSpace($appVersion)) {
  $expectedVersionMarker = $appVersion
  $versionMarkerSource = "appx_package_fallback"
}

$effectiveLocalCodexRoot = if (-not [string]::IsNullOrWhiteSpace($LocalCodexRoot)) {
  $LocalCodexRoot
} else {
  Join-Path $env:LOCALAPPDATA "OpenAI\Codex"
}
$binRoot = Join-Path $effectiveLocalCodexRoot "bin"
$runtimeRoot = Join-Path $effectiveLocalCodexRoot "runtimes\cua_node"

$discoveredCli = Get-ChildItem -LiteralPath $binRoot -Directory -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  ForEach-Object {
    $candidate = Join-Path $_.FullName "codex.exe"
    if (Test-ExistingFile $candidate) { $candidate }
  } |
  Select-Object -First 1

$discoveredRuntime = Get-ChildItem -LiteralPath $runtimeRoot -Directory -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Where-Object {
    (Test-ExistingFile (Join-Path $_.FullName "bin\node_repl.exe")) -and
    (Test-ExistingFile (Join-Path $_.FullName "bin\node.exe")) -and
    (Test-ExistingDir (Join-Path $_.FullName "bin\node_modules"))
  } |
  Select-Object -First 1

$discoveredNodeRepl = if ($discoveredRuntime) {
  Join-Path $discoveredRuntime.FullName "bin\node_repl.exe"
} else { "" }
$discoveredNodePath = if ($discoveredRuntime) {
  Join-Path $discoveredRuntime.FullName "bin\node.exe"
} else { "" }
$discoveredNodeModules = if ($discoveredRuntime) {
  Join-Path $discoveredRuntime.FullName "bin\node_modules"
} else { "" }

$configuredCli = Get-ConfigValue $content "CODEX_CLI_PATH"
$configuredNodePath = Get-ConfigValue $content "NODE_REPL_NODE_PATH"
$configuredNodeModules = Get-ConfigValue $content "NODE_REPL_NODE_MODULE_DIRS"
$configuredVersionMarker = Get-ConfigValue $content "BROWSER_USE_CODEX_APP_VERSION"
$nodeReplCommandMatch = [regex]::Match(
  $content,
  '(?m)^\s*command\s*=\s*[''"]([^''"]*node_repl\.exe)[''"]'
)
$configuredNodeRepl = if ($nodeReplCommandMatch.Success) {
  $nodeReplCommandMatch.Groups[1].Value
} else { "" }

$appInstallRootExists = Test-ExistingDir $effectiveAppInstallRoot
$configuredCliExists = Test-ExistingFile $configuredCli
$configuredCliLeaf = [IO.Path]::GetFileName($configuredCli)
$providerAdapterOverride = $configuredCliExists -and
  $configuredCliLeaf.Equals("codex-provider-adapter.exe", [StringComparison]::OrdinalIgnoreCase)
$cliPathMode = if ($providerAdapterOverride) { "provider_adapter_override" } else { "native_direct" }
$configuredNodeReplExists = Test-ExistingFile $configuredNodeRepl
$configuredNodePathExists = Test-ExistingFile $configuredNodePath
$configuredNodeModulesExists = Test-ExistingDir $configuredNodeModules
$cliPathMatches = $configuredCliExists -and (Test-ExistingFile $discoveredCli) -and
  ($providerAdapterOverride -or (Test-SamePath $configuredCli $discoveredCli))
$nodeReplPathMatches = $configuredNodeReplExists -and (Test-ExistingFile $discoveredNodeRepl) -and
  (Test-SamePath $configuredNodeRepl $discoveredNodeRepl)
$nodePathMatches = $configuredNodePathExists -and (Test-ExistingFile $discoveredNodePath) -and
  (Test-SamePath $configuredNodePath $discoveredNodePath)
$nodeModulesPathMatches = $configuredNodeModulesExists -and (Test-ExistingDir $discoveredNodeModules) -and
  (Test-SamePath $configuredNodeModules $discoveredNodeModules)
$versionMarkerMatches = (-not [string]::IsNullOrWhiteSpace($expectedVersionMarker)) -and
  $configuredVersionMarker.Equals($expectedVersionMarker, [StringComparison]::OrdinalIgnoreCase)

$issues = @()
if (-not $appInstallRootExists) {
  $issues += "codex_app_install_root_missing"
}
if (-not (Test-ExistingFile $discoveredCli)) {
  $issues += "codex_cli_path_discovery_failed"
} elseif (-not $configuredCliExists) {
  $issues += "stale_or_missing_CODEX_CLI_PATH"
} elseif (-not $cliPathMatches) {
  $issues += "CODEX_CLI_PATH_drift"
}
if (-not $discoveredRuntime) {
  $issues += "codex_cua_node_runtime_discovery_failed"
}
if (-not $configuredNodeReplExists) {
  $issues += "stale_or_missing_node_repl_command"
} elseif ($discoveredRuntime -and -not $nodeReplPathMatches) {
  $issues += "node_repl_command_path_drift"
}
if (-not $configuredNodePathExists) {
  $issues += "stale_or_missing_NODE_REPL_NODE_PATH"
} elseif ($discoveredRuntime -and -not $nodePathMatches) {
  $issues += "NODE_REPL_NODE_PATH_drift"
}
if (-not $configuredNodeModulesExists) {
  $issues += "stale_or_missing_NODE_REPL_NODE_MODULE_DIRS"
} elseif ($discoveredRuntime -and -not $nodeModulesPathMatches) {
  $issues += "NODE_REPL_NODE_MODULE_DIRS_drift"
}
if ([string]::IsNullOrWhiteSpace($expectedVersionMarker)) {
  $issues += "codex_app_version_marker_unavailable"
} elseif ([string]::IsNullOrWhiteSpace($configuredVersionMarker)) {
  $issues += "codex_app_version_marker_missing"
} elseif (-not $versionMarkerMatches) {
  $issues += "codex_app_version_marker_drift"
}

$status = if ($issues.Count -eq 0) { "pass" } else { "warn" }
$result = [ordered]@{
  ts = (Get-Date).ToString("o")
  phase = "codex_client_update_check"
  status = $status
  trigger = "explicit_user_reported_codex_update_only"
  scope = "appx_browser_chrome_version_marker_and_configured_vs_discovered_cli_cua_node_paths"
  config_path = $configPath
  appx_version = $appVersion
  app_install_root = $effectiveAppInstallRoot
  app_install_root_exists = $appInstallRootExists
  configured_version_marker = $configuredVersionMarker
  expected_version_marker = $expectedVersionMarker
  version_marker_source = $versionMarkerSource
  version_marker_matches = $versionMarkerMatches
  configured_cli_path = $configuredCli
  configured_cli_path_exists = $configuredCliExists
  discovered_cli_path = [string]$discoveredCli
  cli_path_mode = $cliPathMode
  cli_path_matches = $cliPathMatches
  configured_node_repl_path = $configuredNodeRepl
  configured_node_repl_path_exists = $configuredNodeReplExists
  discovered_node_repl_path = $discoveredNodeRepl
  node_repl_path_matches = $nodeReplPathMatches
  configured_node_path = $configuredNodePath
  configured_node_path_exists = $configuredNodePathExists
  discovered_node_path = $discoveredNodePath
  node_path_matches = $nodePathMatches
  configured_node_modules_path = $configuredNodeModules
  configured_node_modules_path_exists = $configuredNodeModulesExists
  discovered_node_modules_path = $discoveredNodeModules
  node_modules_path_matches = $nodeModulesPathMatches
  issues = @($issues | Select-Object -Unique)
  repair_available = $false
  next_step = if ($issues.Count -eq 0) {
    "stop_no_drift"
  } else {
    "report_exact_drift_and_wait_for_explicit_repair_authorization"
  }
  rule = "Run only after the user explicitly reports a Codex update. Read-only path/version comparison; report drift only and never repair."
}

$result | ConvertTo-Json -Depth 8
if ($status -eq "warn") { exit 2 }

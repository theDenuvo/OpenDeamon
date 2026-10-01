<# OpenDeamon v0.1 bootstrap (Windows PowerShell 5.1+).
   Usage:
     . A:\OpenDeamon\bootstrap.ps1        # set env in current session only
     A:\OpenDeamon\bootstrap.ps1 -z "..." # set env + run hermes with args
   Process-scoped only: touches no system settings, writes nothing to C:.
   OPENROUTER_API_KEY is read from the existing secrets file in-process and
   never printed, logged, or written to disk. #>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$HermesArgs)

$ErrorActionPreference = "Stop"
$Root = "A:\OpenDeamon"

$env:HERMES_HOME = "$Root\hermes-home"
$env:UV_CACHE_DIR = "$Root\cache\uv"
$env:UV_NO_MANAGED_PYTHON = "1"
$env:PIP_CACHE_DIR = "$Root\cache\pip"
$env:NPM_CONFIG_CACHE = "$Root\cache\npm"
$env:PLAYWRIGHT_BROWSERS_PATH = "$Root\cache\ms-playwright"
$env:TEMP = "$Root\cache\tmp"
$env:TMP = "$Root\cache\tmp"

foreach ($d in @($env:UV_CACHE_DIR, $env:PIP_CACHE_DIR, $env:NPM_CONFIG_CACHE,
    $env:PLAYWRIGHT_BROWSERS_PATH, "$Root\cache\tmp")) {
  if (-not (Test-Path -LiteralPath $d)) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
  }
}

# Reuse the already-installed Hermes binaries (no reinstall, no C: writes).
$HermesBin = "A:\hermes\bin"
if ($env:PATH -notlike "*$HermesBin*") { $env:PATH = "$HermesBin;$env:PATH" }

# Local tools on A: (ffmpeg for whisper/media functions).
$ToolsBin = "$Root\tools\ffmpeg"
if ((Test-Path -LiteralPath $ToolsBin) -and ($env:PATH -notlike "*$ToolsBin*")) {
  $env:PATH = "$ToolsBin;$env:PATH"
}

# Ensure the hands bridge is up (start hidden if port is closed).
try {
  $c = New-Object Net.Sockets.TcpClient
  $r = $c.BeginConnect("127.0.0.1", 9131, $null, $null)
  if (-not ($r.AsyncWaitHandle.WaitOne(800) -and $c.Connected)) {
    $pyw = "C:\Users\cheli\AppData\Local\Programs\Python\Python314\pythonw.exe"
    if (Test-Path -LiteralPath $pyw) {
      Start-Process -FilePath $pyw -ArgumentList "$Root\bridge\hands.py" -WindowStyle Hidden
    }
  }
  $c.Close()
} catch { }

# Load cloud keys in-process from the pre-existing secrets files.
# Nothing is printed, logged, or written to disk.
#
# OpenRouter lives in A:\AI\daemon\config\secrets.local.toml ([openrouter].api_key).
$secretsFile = "A:\AI\daemon\config\secrets.local.toml"
$inSection = $false
foreach ($line in (Get-Content -LiteralPath $secretsFile -Encoding UTF8)) {
  $t = $line.Trim()
  if ($t -match '^\[(.+)\]$') { $inSection = ($Matches[1].Trim() -eq "openrouter"); continue }
  if ($inSection -and $t -match '^api_key\s*=\s*["''](.+)["'']$') {
    $env:OPENROUTER_API_KEY = $Matches[1].Trim()
    break
  }
}

# NVIDIA NIM and Groq live in A:\OpenDeamon\secrets\gateway.env (KEY=value).
# Needed by the delegation scheme (TODO phase 2-ter layer 0 item 9): children
# run on NIM, the reviewer on Groq through provider `custom` - Hermes has no
# `groq` provider, so the key must be in the environment under that exact name.
$gatewayEnv = "$Root\secrets\gateway.env"
if (Test-Path -LiteralPath $gatewayEnv) {
  foreach ($line in (Get-Content -LiteralPath $gatewayEnv -Encoding UTF8)) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#") -or $t -notmatch '=') { continue }
    $name, $value = $t -split '=', 2
    $name = $name.Trim(); $value = $value.Trim().Trim('"').Trim("'")
    # Only the two keys this project routes through; OPENAI_API_KEYS is dead
    # (HTTP 401 "Incorrect API key", verified 2026-09-30) and is not loaded.
    if ($name -in @("NVIDIA_API_KEY", "GROQ_API_KEY")) {
      [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
  }
}

if (-not $env:OPENROUTER_API_KEY) { throw "OPENROUTER_API_KEY not found in $secretsFile" }
if (-not $env:NVIDIA_API_KEY) { throw "NVIDIA_API_KEY not found in $gatewayEnv" }
if (-not $env:GROQ_API_KEY) { throw "GROQ_API_KEY not found in $gatewayEnv" }

if ($HermesArgs -and $HermesArgs.Count -gt 0) {
  & "$HermesBin\hermes.exe" @HermesArgs
}

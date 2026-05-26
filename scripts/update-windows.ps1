# AI Mesh updater for Windows installs created with setup-windows.ps1.
# Pulls the latest code, refreshes Python deps, and restarts the Scheduled Task.
#
# Run as Administrator:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\update-windows.ps1

[CmdletBinding()]
param(
    [string] $InstallDir = "C:\ai-mesh",
    [string] $TaskName   = "AI Mesh Server"
)

$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Die($msg)  { Write-Host "XX $msg"  -ForegroundColor Red; exit 1 }

# ── Verify admin ─────────────────────────────────────────────────────────────
$current   = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($current)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die "Must run as Administrator."
}

$appDir  = Join-Path $InstallDir "app"
$venvDir = Join-Path $InstallDir "venv"
$git     = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
$venvPip = Join-Path $venvDir "Scripts\pip.exe"

if (-not (Test-Path (Join-Path $appDir ".git"))) { Die "$appDir is not a git repo (run setup-windows.ps1 first)" }
if (-not (Test-Path $venvPip))                   { Die "venv missing at $venvDir (run setup-windows.ps1 first)" }
if (-not $git)                                    { Die "git not found in PATH" }

Info "Pulling latest code"
& $git -C $appDir pull --ff-only

Info "Refreshing Python dependencies"
& $venvPip install -r (Join-Path $appDir "server\requirements.txt")

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Die "Scheduled task '$TaskName' not found"
}

Info "Stopping task"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Info "Starting task"
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize

Info "Done."

# AI Mesh server installer for Windows.
# Installs deps, generates a self-signed cert (or uses provided),
# registers a Scheduled Task that runs the server at boot as SYSTEM,
# and opens the Windows Firewall for the chosen port.
#
# Run as Administrator from a PowerShell prompt:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\setup-windows.ps1
#
# Or non-interactive:
#   .\setup-windows.ps1 -InstallDir C:\ai-mesh -Port 8443 -TlsMode self-signed

[CmdletBinding()]
param(
    [string] $InstallDir = "C:\ai-mesh",
    [string] $RepoUrl    = "https://github.com/Superfish1000/AI-Mesh.git",
    [int]    $Port       = 8443,
    [ValidateSet("self-signed", "provided", "none")]
    [string] $TlsMode    = "self-signed",
    [string] $CertFile   = "",
    [string] $KeyFile    = "",
    [string] $TaskName   = "AI Mesh Server"
)

$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!! $msg"  -ForegroundColor Yellow }
function Die($msg)  { Write-Host "XX $msg"  -ForegroundColor Red; exit 1 }

# ── Verify admin ─────────────────────────────────────────────────────────────
$current = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($current)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die "Must run as Administrator."
}

# ── Locate Python ────────────────────────────────────────────────────────────
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Die "Python not found. Install Python 3.11+ from https://www.python.org/downloads/ and re-run."
}
Info "Using Python: $python"

# Sanity check Python version (>= 3.11)
$ver = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $ver.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    Die "Python 3.11+ required (found $ver)."
}

# ── Locate git ───────────────────────────────────────────────────────────────
$git = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
if (-not $git) {
    Die "git not found. Install from https://git-scm.com/download/win and re-run."
}

# ── Clone / update repo ──────────────────────────────────────────────────────
$appDir  = Join-Path $InstallDir "app"
$venvDir = Join-Path $InstallDir "venv"

if (-not (Test-Path $InstallDir)) {
    Info "Creating $InstallDir"
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}

if (Test-Path (Join-Path $appDir ".git")) {
    Info "Updating repo at $appDir"
    & $git -C $appDir pull --ff-only
} else {
    Info "Cloning repo to $appDir"
    & $git clone $RepoUrl $appDir
}

# ── Create venv + install deps ───────────────────────────────────────────────
if (-not (Test-Path $venvDir)) {
    Info "Creating Python venv at $venvDir"
    & $python -m venv $venvDir
}
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPip    = Join-Path $venvDir "Scripts\pip.exe"

Info "Installing Python dependencies"
& $venvPython -m pip install --upgrade pip
& $venvPip install -r (Join-Path $appDir "server\requirements.txt")

# ── TLS material ─────────────────────────────────────────────────────────────
$serverDir = Join-Path $appDir "server"
$targetCert = Join-Path $serverDir "cert.pem"
$targetKey  = Join-Path $serverDir "key.pem"
$sslArgs    = @()

switch ($TlsMode) {
    "self-signed" {
        Info "Generating self-signed certificate"
        Push-Location $serverDir
        try { & $venvPython "gen_cert.py" } finally { Pop-Location }
        $sslArgs = @("--ssl-certfile", $targetCert, "--ssl-keyfile", $targetKey)
    }
    "provided" {
        if (-not (Test-Path $CertFile)) { Die "CertFile not found: $CertFile" }
        if (-not (Test-Path $KeyFile))  { Die "KeyFile not found: $KeyFile" }
        Copy-Item $CertFile $targetCert -Force
        Copy-Item $KeyFile  $targetKey  -Force
        $sslArgs = @("--ssl-certfile", $targetCert, "--ssl-keyfile", $targetKey)
    }
    "none" { $sslArgs = @() }
}

# ── Firewall rule ────────────────────────────────────────────────────────────
$fwRuleName = "AI Mesh ($Port)"
if (Get-NetFirewallRule -DisplayName $fwRuleName -ErrorAction SilentlyContinue) {
    Info "Firewall rule '$fwRuleName' already present"
} else {
    Info "Opening Windows Firewall for TCP $Port"
    New-NetFirewallRule -DisplayName $fwRuleName -Direction Inbound `
        -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}

# ── Scheduled Task (runs at boot as SYSTEM) ──────────────────────────────────
$uvicorn = Join-Path $venvDir "Scripts\uvicorn.exe"
$argList = @("server:app", "--host", "0.0.0.0", "--port", "$Port") + $sslArgs
$argString = ($argList | ForEach-Object {
    if ($_ -match '\s') { "`"$_`"" } else { $_ }
}) -join ' '

Info "Registering scheduled task '$TaskName'"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action    = New-ScheduledTaskAction -Execute $uvicorn -Argument $argString -WorkingDirectory $serverDir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "AI Mesh coordination server" | Out-Null

Info "Starting task"
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 3

# ── Show bootstrap URL ───────────────────────────────────────────────────────
# Scheduled tasks running as SYSTEM don't have a console, so the setup token
# from server.py's stdout isn't visible. Instead, query the server directly.
$scheme = if ($TlsMode -eq "none") { "http" } else { "https" }
$url    = "${scheme}://localhost:$Port"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " AI Mesh is running." -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host " URL:           $url"
Write-Host " Install dir:   $InstallDir"
Write-Host " Task name:     $TaskName"
Write-Host ""
Write-Host " To find the FIRST-RUN setup token, check the server's stderr:"
Write-Host "   Get-WinEvent -LogName Application -MaxEvents 50 | ?{ \$_.Message -match 'setup.token' }"
Write-Host ""
Write-Host " Or stop the task, run the server in foreground once to see it:"
Write-Host "   Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "   & '$uvicorn' $argString"
Write-Host "   # copy the /setup?token=... URL, Ctrl+C, then:"
Write-Host "   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host " Service control:"
Write-Host "   Get-ScheduledTask  -TaskName '$TaskName'"
Write-Host "   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "   Stop-ScheduledTask  -TaskName '$TaskName'"
Write-Host ""

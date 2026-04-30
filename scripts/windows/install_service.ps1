#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install PGMRec as a Windows service using NSSM.

.DESCRIPTION
    Creates a 'PGMRec' Windows service that runs the FastAPI backend via uvicorn
    in the Python virtual environment.

    PGMRec is a LAN-only application. By default the service binds to 127.0.0.1
    (localhost only). To expose it to other machines on your LAN, pass -Host 0.0.0.0
    and set PGMREC_CORS_ORIGINS to your server's LAN IP in C:\PGMRec\.env.

    Prerequisites:
      - Python 3.12+ installed and on PATH
      - FFmpeg installed (set PGMREC_FFMPEG_PATH_OVERRIDE in .env)
      - NSSM (Non-Sucking Service Manager):
          Option A — automatic download (requires internet once during setup):
            The script downloads nssm.exe to scripts\windows\nssm.exe automatically.
          Option B — offline / pre-downloaded:
            Place nssm.exe in scripts\windows\nssm.exe before running this script.
            Download from: https://nssm.cc/release/nssm-2.24.zip  (one-time, any machine)
      - Run from repository root as Administrator

.EXAMPLE
    # Local machine only (default)
    powershell -ExecutionPolicy Bypass -File scripts\windows\install_service.ps1

    # Expose to LAN (set -Host 0.0.0.0 and configure CORS in .env)
    powershell -ExecutionPolicy Bypass -File scripts\windows\install_service.ps1 -Host 0.0.0.0
#>

param(
    [string]$InstallDir = "C:\PGMRec",
    [string]$ServiceName = "PGMRec",
    [string]$ServiceUser = "LocalSystem",
    [string]$Host = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

Write-Host "=== PGMRec Windows Service Installer ===" -ForegroundColor Cyan
Write-Host "Repository : $RepoDir"
Write-Host "Install dir: $InstallDir"

# ── Helper: download NSSM ─────────────────────────────────────────────────────
function Get-Nssm {
    $nssmPath = "$PSScriptRoot\nssm.exe"
    if (Test-Path $nssmPath) { return $nssmPath }

    Write-Host "Downloading NSSM…"
    $nssmZip = "$env:TEMP\nssm.zip"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" `
                      -OutFile $nssmZip -UseBasicParsing
    $nssmExtract = "$env:TEMP\nssm_extract"
    Expand-Archive -Path $nssmZip -DestinationPath $nssmExtract -Force
    $nssmBin = Get-ChildItem -Path $nssmExtract -Recurse -Filter "nssm.exe" |
               Where-Object { $_.DirectoryName -like "*win64*" } |
               Select-Object -First 1
    Copy-Item $nssmBin.FullName $nssmPath
    Write-Host "NSSM installed to $nssmPath"
    return $nssmPath
}

# ── 1. Create install layout ──────────────────────────────────────────────────
$dirs = @(
    "$InstallDir\backend",
    "$InstallDir\data\channels",
    "$InstallDir\data\manifests",
    "$InstallDir\data\exports",
    "$InstallDir\data\preview",
    "$InstallDir\logs\exports"
)
foreach ($d in $dirs) { New-Item -ItemType Directory -Path $d -Force | Out-Null }

Write-Host "Copying backend…"
$robocopyFlags = "/E /XD __pycache__ .venv /XF *.pyc *.pyo /NFL /NDL /NJH"
$robocopyArgs = "`"$RepoDir\backend`" `"$InstallDir\backend`" $robocopyFlags"
Start-Process robocopy -ArgumentList $robocopyArgs -Wait -NoNewWindow

# Optional frontend
$frontendDist = "$RepoDir\frontend\dist"
if (Test-Path $frontendDist) {
    New-Item -ItemType Directory -Path "$InstallDir\frontend\dist" -Force | Out-Null
    robocopy $frontendDist "$InstallDir\frontend\dist" /E /NFL /NDL /NJH | Out-Null
    Write-Host "Frontend build copied."
} else {
    Write-Host "NOTE: No frontend\dist found. Run 'cd frontend && npm run build' first."
}

# ── 2. Python venv ────────────────────────────────────────────────────────────
$venvDir = "$InstallDir\.venv"
if (-not (Test-Path $venvDir)) {
    Write-Host "Creating Python venv…"
    python -m venv $venvDir
}
& "$venvDir\Scripts\pip.exe" install --quiet --upgrade pip
& "$venvDir\Scripts\pip.exe" install --quiet -r "$InstallDir\backend\requirements.txt"
Write-Host "Python dependencies installed."

# ── 3. .env file ──────────────────────────────────────────────────────────────
$envFile = "$InstallDir\.env"
if (-not (Test-Path $envFile)) {
    Copy-Item "$RepoDir\.env.example" $envFile
    Write-Host "Created $envFile from template."
    Write-Host "⚠️  Edit $envFile — set PGMREC_JWT_SECRET_KEY and PGMREC_ADMIN_PASSWORD." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists — not overwritten."
}

# ── 4. Register Windows service via NSSM ─────────────────────────────────────
$nssm = Get-Nssm
$uvicorn = "$venvDir\Scripts\uvicorn.exe"

# Remove old service if it exists
$existing = sc.exe query $ServiceName 2>$null
if ($existing) {
    Write-Host "Removing existing $ServiceName service…"
    & $nssm stop $ServiceName confirm 2>$null
    & $nssm remove $ServiceName confirm 2>$null
}

Write-Host "Installing service '$ServiceName'…"
& $nssm install $ServiceName $uvicorn
& $nssm set $ServiceName AppParameters "app.main:app --host $Host --port $Port --workers 1"
& $nssm set $ServiceName AppDirectory "$InstallDir\backend"
& $nssm set $ServiceName AppEnvironmentExtra "PYTHONUNBUFFERED=1"
& $nssm set $ServiceName AppEnvFile "$envFile"
& $nssm set $ServiceName DisplayName "PGMRec Recording System"
& $nssm set $ServiceName Description "Broadcast recording, compliance and preview (pgm-rec)"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout "$InstallDir\logs\service-stdout.log"
& $nssm set $ServiceName AppStderr "$InstallDir\logs\service-stderr.log"
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 10485760  # 10 MB

Write-Host ""
Write-Host "=== Installation complete ===" -ForegroundColor Green
Write-Host "  Bound to : $Host`:$Port"
if ($Host -eq "127.0.0.1") {
    Write-Host "  Access   : http://localhost:$Port  (local machine only)" -ForegroundColor Cyan
    Write-Host "  LAN tip  : To expose to other LAN machines, re-run with -Host 0.0.0.0" -ForegroundColor DarkYellow
    Write-Host "             and set PGMREC_CORS_ORIGINS=http://<your-LAN-IP>:$Port in $envFile" -ForegroundColor DarkYellow
} else {
    Write-Host "  Access   : http://<your-LAN-IP>:$Port" -ForegroundColor Cyan
    Write-Host "  ⚠ Ensure PGMREC_CORS_ORIGINS is set to your LAN IP in $envFile" -ForegroundColor Yellow
}
Write-Host "  Edit env : $envFile"
Write-Host "  Start    : sc start $ServiceName  (or: scripts\windows\start_service.ps1)"
Write-Host "  Stop     : sc stop  $ServiceName  (or: scripts\windows\stop_service.ps1)"
Write-Host "  Logs     : $InstallDir\logs\"

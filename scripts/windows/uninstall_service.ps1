#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Uninstall the PGMRec Windows service.

.DESCRIPTION
    Stops and removes the PGMRec service registered by install_service.ps1.
    Data, logs, and the virtual environment are NOT removed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_service.ps1
#>

param(
    [string]$ServiceName = "PGMRec"
)

$ErrorActionPreference = "Stop"
$nssmPath = "$PSScriptRoot\nssm.exe"

function Get-ServiceStatus($name) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    return $svc
}

$svc = Get-ServiceStatus $ServiceName
if (-not $svc) {
    Write-Host "Service '$ServiceName' not found — nothing to do."
    exit 0
}

if ($svc.Status -eq "Running") {
    Write-Host "Stopping service '$ServiceName'…"
    if (Test-Path $nssmPath) {
        & $nssmPath stop $ServiceName confirm
    } else {
        Stop-Service -Name $ServiceName -Force
    }
}

Write-Host "Removing service '$ServiceName'…"
if (Test-Path $nssmPath) {
    & $nssmPath remove $ServiceName confirm
} else {
    sc.exe delete $ServiceName
}

Write-Host ""
Write-Host "=== Service '$ServiceName' removed ===" -ForegroundColor Green
Write-Host "Data/logs/venv are preserved. Remove C:\PGMRec manually if no longer needed."

#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Start the PGMRec Windows service.
#>

param([string]$ServiceName = "PGMRec")

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Error "Service '$ServiceName' not found. Run install_service.ps1 first."
    exit 1
}

if ($svc.Status -eq "Running") {
    Write-Host "Service '$ServiceName' is already running."
    exit 0
}

Start-Service -Name $ServiceName
Write-Host "Service '$ServiceName' started." -ForegroundColor Green

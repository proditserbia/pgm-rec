#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Stop the PGMRec Windows service.
#>

param([string]$ServiceName = "PGMRec")

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Error "Service '$ServiceName' not found."
    exit 1
}

if ($svc.Status -eq "Stopped") {
    Write-Host "Service '$ServiceName' is already stopped."
    exit 0
}

Stop-Service -Name $ServiceName -Force
Write-Host "Service '$ServiceName' stopped." -ForegroundColor Yellow

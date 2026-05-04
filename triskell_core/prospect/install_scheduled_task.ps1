# Installe la tâche planifiée Windows pour la boucle nocturne Triskell.
# Lance C:\path\to\python.exe -m triskell_core.prospect.nightly chaque nuit à 03:00.
#
# Usage : clic droit sur le .ps1 → "Exécuter avec PowerShell" (admin pas requis)
# ou : powershell -ExecutionPolicy Bypass -File install_scheduled_task.ps1
#
# Pour désinstaller :
#   Unregister-ScheduledTask -TaskName "Triskell Prospect Nightly" -Confirm:$false

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
# Remonte de prospect/ → triskell_core/ → Triskell Core/ (racine du package importable)
$CoreRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$Python = "C:\Users\jorda\AppData\Local\Programs\Python\Python312\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "Python introuvable à $Python. Adapte la variable `$Python en haut du script."
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m triskell_core.prospect.nightly --mon-prenom Jordan" `
    -WorkingDirectory $CoreRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At 3am

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U

$TaskName = "Triskell Prospect Nightly"

# Si la tâche existe déjà, on la met à jour
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Set-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings
    Write-Host "[ok] Tâche mise à jour : $TaskName"
} else {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description "Boucle nocturne Triskell Prospection : poll IMAP + envois + relances"
    Write-Host "[ok] Tâche créée : $TaskName"
}

Write-Host ""
Write-Host "Programmé pour : tous les jours à 03:00"
Write-Host "Working dir    : $CoreRoot"
Write-Host "Logs           : $env:USERPROFILE\.triskell-prospect\nightly.log"
Write-Host ""
Write-Host "Pour tester maintenant :"
Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "Pour désinstaller :"
Write-Host "  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"

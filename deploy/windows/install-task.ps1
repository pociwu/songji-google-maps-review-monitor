param(
    [Parameter(Mandatory=$true)][string]$ProjectPath,
    [string]$CondaExe = "$env:USERPROFILE\miniconda3\Scripts\conda.exe"
)

$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$arguments = "run --no-capture-output -n maps-review-monitor maps-review-monitor --config `"$ProjectPath\config.toml`" check"
$action = New-ScheduledTaskAction -Execute $CondaExe -Argument $arguments -WorkingDirectory $ProjectPath
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 12)
Register-ScheduledTask -TaskName "MapsReviewMonitor" -Action $action -Trigger $trigger -Settings $settings -Description "Google Maps review monitor" -Force
Write-Host "MapsReviewMonitor 工作排程已建立。"

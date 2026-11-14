# 毎朝決まった時刻に run_daily.ps1 を実行するタスクを登録する(Windows)。
# PC が起動していなかった日は、次に起動したときに実行される。
# 使い方: .\register_task.ps1            (毎朝 7:30)
#         .\register_task.ps1 -At 21:00  (時刻を変える)
# 解除:   Unregister-ScheduledTask -TaskName "Claude資産運用" -Confirm:$false
param([string]$At = "7:30")
$pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $pwsh) { $pwsh = (Get-Command powershell).Source }  # PowerShell 7 が無ければ標準の PowerShell を使う
$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\run_daily.ps1`"" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "Claude資産運用" -Action $action -Trigger $trigger `
    -Settings $settings -Description "ペーパートレードの1日1回の自動運用" -Force

# 建立 Windows 工作排程「HEMS 夜間自動任務」：在指定時間執行一次 night_auto.bat。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\night\register_task.ps1 [-At "2026-10-16 22:00"]
param([datetime]$At = (Get-Date).Date.AddHours(22))

$name = "HEMS 夜間自動任務"
$bat = "C:\hems\scripts\night\night_auto.bat"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory "C:\hems"
$trigger = New-ScheduledTaskTrigger -Once -At $At
# 排程本身的上限設 2 小時（.bat 內的 90 分鐘限制會先生效，這是第二道保險）；
# 電腦睡眠時喚醒執行；錯過時間（例如當時關機）開機後不補跑，避免白天突然開始改程式。
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "HEMS 專案：在 night-auto 分支（C:\hems-night）自動做下一個未完成項目，最長 90 分鐘。" -Force | Out-Null
$task = Get-ScheduledTask -TaskName $name
$info = $task | Get-ScheduledTaskInfo
"已建立排程：$name"
"下次執行：$($info.NextRunTime)"
"狀態：$($task.State)"

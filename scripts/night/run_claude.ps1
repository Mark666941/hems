# 以受限權限、限時執行 Claude Code（由 night_auto.bat 呼叫，時間上限由 .bat 傳入）。
# 權限：dontAsk 模式＋不回應權限詢問 → 只有下面「允許清單」的工具能用，其餘一律自動拒絕。
param(
    [Parameter(Mandatory)] [int]$Minutes,
    [Parameter(Mandatory)] [string]$PromptFile,
    [Parameter(Mandatory)] [string]$WorkDir,
    [Parameter(Mandatory)] [string]$Log,
    [string]$ReportPath = ""          # 有值時：Claude 沒寫報告（例如逾時）就由這裡補一份
)

function Write-FallbackReport([string]$reason) {
    if (-not $ReportPath) { return }
    if ((Test-Path $ReportPath) -and ((Get-Item $ReportPath).LastWriteTime -gt $start)) { return }
    New-Item -ItemType Directory -Force (Split-Path $ReportPath) | Out-Null
    @"
# 夜間自動任務報告（由排程腳本自動補寫）

- 時間：$($start.ToString('yyyy-MM-dd HH:mm')) 開始
- 結果：$reason
- Claude Code 沒有寫出報告，這次的修改可能不完整，**沒有 commit 的修改不要直接採用**。

## 建議明天先檢查
1. 執行紀錄：``$Log``、``$Log.out``、``$Log.err``
2. 在 ``$WorkDir`` 執行 ``git status`` 與 ``git log --oneline -5``，看有沒有未 commit 的修改
3. 若要放棄未 commit 的修改：``git restore .`` 與 ``git clean -fd``
"@ | Out-File $ReportPath -Encoding utf8
}

$allowed = @(
    'Read(./**)', 'Edit(./**)', 'Write(./**)', 'Glob', 'Grep',   # 只能讀寫工作資料夾內的檔案
    'Bash(git *)',                                               # git 指令
    'Bash(python -m pytest*)', 'Bash(pytest*)'                   # 執行測試
)
$denied = @(
    'Bash(git push*)', 'Bash(git pull*)', 'Bash(git fetch*)', 'Bash(git merge*)', 'Bash(git rebase*)',
    'Bash(git checkout main*)', 'Bash(git switch main*)', 'Bash(git reset --hard*)', 'Bash(git branch -D*)',
    'Bash(git worktree*)', 'Bash(git config*)', 'Bash(git remote*)',
    'WebFetch', 'WebSearch', 'PowerShell', 'Agent', 'Workflow', 'NotebookEdit'
)
function Quote([string]$s) { '"' + $s.Replace('"', '\"') + '"' }
$argList = @('-p', '--permission-mode', 'dontAsk', '--permission-prompts', 'none', '--output-format', 'text',
             '--allowedTools') + ($allowed | ForEach-Object { Quote $_ }) +
           @('--disallowedTools') + ($denied | ForEach-Object { Quote $_ })

$claude = (Get-Command claude -ErrorAction Stop).Source
$start = Get-Date
"[$($start.ToString('yyyy-MM-dd HH:mm:ss'))] 啟動 Claude Code（上限 $Minutes 分鐘）於 $WorkDir" | Out-File $Log -Encoding utf8
$p = Start-Process -FilePath $claude -ArgumentList $argList -WorkingDirectory $WorkDir -NoNewWindow -PassThru `
        -RedirectStandardInput $PromptFile -RedirectStandardOutput "$Log.out" -RedirectStandardError "$Log.err"

if (-not $p.WaitForExit($Minutes * 60 * 1000)) {
    # 超過時間上限：強制結束 Claude Code 與它啟動的所有子程序（pytest、git 等）
    & taskkill.exe /PID $p.Id /T /F | Out-Null
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 超過 $Minutes 分鐘，已強制結束" | Out-File $Log -Append -Encoding utf8
    Write-FallbackReport "超過 $Minutes 分鐘時間上限，已強制結束"
    exit 124
}
$code = $p.ExitCode
$elapsed = [int]((Get-Date) - $start).TotalMinutes
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Claude Code 結束，代碼 $code，耗時 $elapsed 分鐘" | Out-File $Log -Append -Encoding utf8
Write-FallbackReport "Claude Code 結束（代碼 $code）但沒有寫報告"
exit $code

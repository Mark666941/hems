# 夜間自動任務

Windows 工作排程器在指定時間執行 `night_auto.bat`，讓 Claude Code 無人看管地完成 **CLAUDE.md 中下一個未完成的項目**（一次只做一項）。

## 檔案

| 檔案 | 作用 |
|---|---|
| `night_auto.bat` | 排程執行的入口：建立 night-auto 分支與獨立工作資料夾、設定虛擬環境、限時 90 分鐘執行 |
| `run_claude.ps1` | 以「只允許清單內工具」的權限啟動 Claude Code，逾時強制結束；沒有報告時補寫一份 |
| `night_prompt.md` | 給 Claude Code 的工作說明（任務、限制、停止規則、報告格式） |
| `dryrun_prompt.md` | 啟動測試用：只回報分支與 pytest 版本，不改任何檔案 |
| `register_task.ps1` | 建立工作排程 |

## 安全設計
- **不動 main**：使用 `git worktree`，night-auto 分支在 `C:\hems-night` 獨立資料夾工作；`C:\hems` 與 main 完全不受影響。
- **權限只開放**：讀寫工作資料夾內檔案、`python -m pytest`、`git`（禁止 push、pull、merge、切回 main、reset --hard 等）。
  使用 `--permission-mode dontAsk --permission-prompts none`：清單外的工具一律自動拒絕；**沒有使用任何跳過權限確認的選項**。
- **時間上限**：`.bat` 的 `TIMEOUT_MIN=90`，逾時強制結束整個程序樹；工作排程另設 2 小時上限作為第二道保險。

## 結果在哪裡
- 報告：`C:\hems-night\docs\night_report.md`
- 執行紀錄：`C:\hems\output\night\night_<時間>.log`（`.out` 為 Claude 輸出、`.err` 為錯誤、`.bat.txt` 為 .bat 流程）
- 程式修改：`C:\hems-night` 的 night-auto 分支（`git log --oneline main..night-auto`）

## 常用操作
```powershell
scripts\night\night_auto.bat --dry-run                                        # 啟動測試（不改檔案）
powershell -ExecutionPolicy Bypass -File scripts\night\register_task.ps1      # 建立今晚 22:00 的排程
Get-ScheduledTask -TaskName "HEMS 夜間自動任務" | Get-ScheduledTaskInfo         # 查看下次執行時間與上次結果
Unregister-ScheduledTask -TaskName "HEMS 夜間自動任務" -Confirm:$false          # 取消排程
git merge night-auto                                                          # 檢查無誤後，把夜間成果合併回 main
git worktree remove C:\hems-night                                             # 不再需要時移除獨立資料夾
```

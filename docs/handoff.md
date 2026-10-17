# 交接紀錄（2026-10-17）

## 目前狀態
- main 最新已推上 GitHub；259 項測試全部通過；GitHub Actions 通過
- 網站：https://hems-chong-he-jian.streamlit.app/ （Supabase 資料庫；keepalive 每 3 天）
- 專案報告：docs/project_report.md；部署說明：docs/deploy.md；安裝程式：python packaging/build_installer.py

## 本階段已完成（優化與修正）
1. 修正：計算子程序重跑整個網頁程式（core/parallel.py `_plain_main`），載入設定檔 13 → 1 次
2. 修正：分析頁模式比較偶發錯誤（isinstance → 屬性判斷）
3. 清理：移除未使用的 Tariff.plan_names、隱藏 Deploy 按鈕、GitHub Actions 升版、重新產生過時報告
4. 移除夜間自動任務（檔案、工作排程、C:\hems-night、night-auto 分支）

## 進行中（未完成，已 git stash）
- **手機版平面圖改版**：`git stash list` 中的「手機版平面圖：圖示與文字依縮放比例縮小」
  - 內容：ui/floorplan_frontend/index.html 新增 UI_K（依 px/m 縮放 0.55–1），家電、人物、房間文字一起縮放；
    窄螢幕隱藏尺寸標註與功率標籤
  - 尚未做：在瀏覽器驗證（電腦寬度、390px 手機寬度、首頁精簡模式、拖曳家電與人物時位置是否正確）
  - 恢復：`git stash pop`，驗證後再測試並 commit

## 待辦清單（使用者已選定，依建議順序）
| 順序 | 項目 | 說明 |
|---|---|---|
| 1 | 手機版平面圖 | 見上方 stash，驗證後提交 |
| 2 | **雲端開啟加速**（最優先） | 修正子程序問題後實測：示範帳號 55 → 36 秒，**平面圖頁 183 秒**（最慢）、分析 3 秒、預測 13 秒。需先找出平面圖頁在雲端單核心為何這麼慢（可能是一天動畫 day_frames、或第一次平面圖觸發的計算／平行子程序啟動），方向：預先計算、延後載入、雲端關閉平行 |
| 3 | 預測頁與表格細節 | 預測頁單核心 3.8 秒（逐日推演兩個月）→ 快取或平行；情境比較表「結論」欄文字被截斷 |
| 4 | MILP＋局部搜尋混合最佳化 | 以 MILP 解為起點，在完整推演上逐時微調；用 scripts/compare_optimizers.py 比較 |
| 5 | 匯入台電智慧電表資料＋自動校正 | 上傳台電 15 分鐘用電 CSV → 與模擬比對 → 自動校正參數（ASHRAE Guideline 14） |
| 6 | 明日排程建議 | 依 Open-Meteo 明天預報算出冷氣設定與家電時間，整理成建議卡 |
| 7 | 簡報模式 | 面試展示導覽：平面圖動畫 → 模式比較 → 整月分析 → MILP 比較 |
| 8 | 英文介面 | 中英切換（工作量最大，最後做） |

## 注意事項
- 截圖／瀏覽器測試一律設 HEMS_DATABASE_URL 指向暫存資料庫，不要動 data/user/hems.db
- 更換資料庫密碼時，Streamlit Secrets 與 GitHub Secret（HEMS_DATABASE_URL）都要改
- 使用者在 GitHub 網頁上改過檔案時，本機要先 pull 再修改

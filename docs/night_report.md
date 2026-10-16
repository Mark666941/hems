# 夜間自動任務報告（2026-10-16，22:00 開始）

分支：`night-auto`（worktree `C:\hems-night`），沒有 push、沒有 merge。
這次做完 **12 項**（全部測試通過、各自一個 commit、CLAUDE.md 已勾選）。22:51 做完第 12 項時，
剩下的項目都需要網路或標示「白天工作負責」，所以停止。

## 1. 完成的項目（依完成順序）

### ① 介面改版第 5 批：家電使用紀錄＋預測頁　`4fe3f0c`
- `core/storage.py`：新增 `usage_records` 資料表（每個設定檔各一份：每週習慣／每日紀錄／帳單），以及新增、列出、刪除的方法（會檢查設定檔屬於目前使用者）
- `ui/usage.py`（新）：預測計算＝物理模擬（一般習慣）＋有紀錄的家電改用「功率 × 時數」＋帳單修正係數，並給出範圍；沒有天氣資料的月份用最接近月份的天氣，電費仍依該月電價算
- `ui/pages/forecast.py`：四張預測卡（本月／下月的用電與電費，含範圍）、每日預測折線、帳單與模擬比對（要不要修正由使用者決定）、紀錄的新增與刪除
- `ui/context.py`、`ui/charts.py`、`app.py`（`forecast_correction`）、`README.md`、`tests/test_forecast.py`（新）

### ② 介面改版第 6a 批：整月分析折線圖＋數據分析式建議　`32f94ef`
- `ui/insights.py`（新）：整月每日資料表；冷氣用電對日均溫做迴歸（斜率、R²）、找出用電最多的日子、比較平日與假日、計算尖峰占比、看所選模式在哪些日子省最多
- `ui/pages/analysis.py`：各模式每日用電折線、累積電費、溫度與冷氣用電散布圖（含迴歸線）、數據分析卡片、每日資料 CSV；原本的長條圖改收進展開區
- `ui/charts.py`、`ui/context.py`、`tests/test_insights.py`（新）

### ③ 優化 1：D1 ruff＋D2 GitHub Actions　`acf193f`
- `ruff.toml`（新）：E/W/F/I/UP/B/SIM 規則；全專案修到 `ruff check .` 全部通過（只動 import 排序、未使用的 import、型別標註的引號、未使用的迴圈變數，**計算結果不變**）
- `.github/workflows/tests.yml`（新）：push 到 main 或開 PR 時自動跑 ruff 與 pytest
- `README.md` 加上徽章；`.gitignore` 加入 `.ruff_cache`

### ④ 優化 3：B1b 混合用電預測模型　`e1dcaff`
- `scripts/ml_forecast.py`（新）：以全國每戶平均用電為目標，比較「去年同月」「純物理」「純 ML（梯度提升樹）」「混合（物理＋ML 修正殘差）」四種方法；用時間序列交叉驗證（5 折、每折預測未來 12 個月），算 MAE／MAPE，並輸出報告與圖
- `tests/test_ml_forecast.py`（新）：檢查特徵、確認沒有偷看未來、混合模型比純物理準、報告與圖能產生
- `data/public/README.md` 補上用法

### ⑤ 優化 5：C2 平面圖一天動畫播放　`6d1853e`
- `ui/pages/plan.py`：`day_frames()` 把已經算好的 24 小時結果整理成逐時畫格（室溫、家電、人物、累計用電）
- `ui/floorplan_frontend/index.html`：播放／暫停、拖曳時間軸、三段速度、回到模擬時間；播放時唯讀，右側面板顯示當下狀態與 24 小時用電長條
- `tests/test_playback.py`（新）

### ⑥ 優化 6：C1 新手引導精靈　`d67c9e3`
- `ui/wizard.py`（新）：四個步驟（房型、成員與作息、電價、熱水器），完成時一次套用設定
- `app.py`：左側選單新增「🧭 新手引導」，`DEFAULT_UI` 加上 `onboarded`；`ui/pages/home.py`：還沒做完引導時顯示提示（可以略過）
- `tests/test_wizard.py`（新）

### ⑦ 優化 7：B2 用電異常偵測　`9bddc21`
- `ui/anomaly.py`（新）：用穩健統計（中位數、1.4826×MAD）看「實測 − 模擬」的誤差，偵測三種異常：單一小時、持續偏離、整天偏離；模型整體偏高或偏低（系統性偏差）不算異常
- `ui/pages/analysis.py`：新增「異常偵測」分頁；`ui/context.py`、`ui/charts.py`、`tests/test_anomaly.py`（新）

### ⑧ 優化 10：C4 情境比較（What-if）與投資回收　`f138c25`
- `ui/whatif.py`（新）：四個情境——冷氣 COP 3.6→4.5、照明換 LED、熱泵熱水器、冷氣設定 +1°C；用取樣推演估算一整年，算出回收年限與使用年限內的淨效益
- `ui/settings.py`：`build_analyzer()` 可以傳入負載模型參數；`ui/usage.py`：`simulate_month()` 可以取樣（`step`）
- `ui/pages/analysis.py`：新增「情境比較」分頁；`ui/context.py`、`ui/charts.py`、`tests/test_whatif.py`（新）

### ⑨ 優化 13：C3 手機版排版　`962978d`
- `ui/theme.py`：螢幕寬度 ≤ 640px 與 ≤ 380px 兩個斷點；`ui/floorplan_frontend/index.html`：≤ 520px 斷點；`tests/test_mobile.py`（新）

### ⑩ 優化 14：C5 報告匯出 PDF　`ad86acd`
- `ui/pdf_report.py`（新）：用 matplotlib 的 PdfPages 產生 A4 報告（嵌入中文字型）：摘要與比較表、三張圖、節省建議（太長會自動換頁）
- `ui/pages/analysis.py`：整月分析新增「下載分析報告（PDF）」；`ui/context.py`、`tests/test_pdf_report.py`（新）

### ⑪ 優化 15：D4 錯誤處理與記錄＋D5 程式文件　`c701e5c`
- `ui/logs.py`（新）：記錄檔寫在 `data/user/logs/hems.log`（輪替保存）；`guarded()` 讓單一頁面出錯時只顯示錯誤訊息，程式不會整個當掉
- `app.py`：每個頁面都包在 `guarded()` 裡；`ui/account.py`：自動存檔失敗時提示並重試，並記錄帳號相關事件（不記錄密碼）
- `tests/conftest.py`：測試的記錄檔寫到暫存資料夾（**沒有改任何測試的判斷**）；`tests/test_logs.py`（新）
- `docs/developer_guide.md`（新）：執行流程、模組地圖、設定字典、常見修改方式、錯誤記錄、測試

### ⑫ 優化 16：E2 示範帳號與示範資料　最後一個 commit（和本報告一起提交）
- `ui/demo.py`（新）：兩個示範設定檔（四口之家含紀錄與帳單；小家庭使用瓦斯熱水器與累進電價）
- `ui/account.py`：登入畫面新增「以示範帳號試用（免註冊）」，每次進入都會重設；桌面版可以「載入示範設定檔」
- `core/storage.py`：新增 `passwordless_user()`、`reset_user()`；`tests/test_demo.py`（新）

## 2. 測試結果
- 最後一次完整執行：**211 passed、0 failed**（`python -m pytest -q -p no:warnings`，約 74 秒）
- `python -m ruff check .`：All checks passed
- 既有測試都沒有刪除、跳過或放寬

## 3. 卡住的地方與原因
沒有任何項目失敗；以下是這次環境沒辦法驗證的部分：
- **B1b 報告沒有產生**：這個環境只允許執行 pytest，不能執行 `python scripts/ml_forecast.py`，所以 `docs/reports/ml_forecast.md` 和 `docs/images/ml_forecast.png` 還沒有產生。程式已經用測試驗證過，包含「混合模型的 MAPE 比純物理低」，但具體數字要請作者自己跑一次才看得到。
- **畫面外觀沒有用眼睛確認**：平面圖動畫（JavaScript）、手機版 CSS、PDF 版面都沒辦法開瀏覽器或 PDF 檢視器看。這次只用測試確認資料、元素與 CSS 結構正確，環境裡也沒有 Node.js 可以檢查 JS 語法。
- **GitHub Actions 還沒跑過**：沒有 push。CI 在 Linux 上執行，可能沒有中文字型，matplotlib 會出現警告但不影響測試結果，不過還是要等第一次執行才能確認。
- **Streamlit 版本**：新手引導用到 `st.dialog(on_dismiss=...)`，需要比較新的 Streamlit。目前虛擬環境的版本可以用，但 `requirements.txt` 寫的是 `streamlit>=1.40`，建議改成目前安裝的版本號，避免部署時裝到太舊的版本。
- 跳過的項目：第 6b 批（需要網路）；優化 4、8、9、11、12、17、18（白天工作負責）；階段 8 部署、階段 9。

## 4. 建議明天先檢查
啟動方式：在 `C:\hems-night` 執行 `C:\hems\.venv\Scripts\python.exe -m streamlit run app.py`（桌面捷徑開的是 `C:\hems` 的 main，看不到今晚的修改）。

1. **跑 B1b**：`C:\hems\.venv\Scripts\python.exe scripts\ml_forecast.py` → 打開 `docs/reports/ml_forecast.md`，看四種模型的 MAPE，以及圖上的線是否合理（大約 15 秒）。
2. **首頁**：會出現「第一次使用？」提示（舊的設定檔也會出現一次）→ 按「開始設定」，走完四步 → 改成瓦斯熱水器後按完成，今日用電應該會下降。
3. **平面圖**：上方按「▶ 播放一天」→ 室溫顏色、家電、人物應該從 0 時跑到 23 時；拖曳時間軸、切換速度、按「回到模擬時間」後要能恢復拖曳與遙控。
4. **預測頁**：新增每週習慣（例如客廳電視平日 19–23 時）和一張帳單 → 預測卡與「帳單與模擬比對」會更新；開啟「依帳單修正預測」後數字會乘上修正係數。也可以從左側「👤 帳號與設定檔」→「🧪 載入示範設定檔」直接看填好資料的樣子。
5. **分析頁**：
   - 「整月分析與建議」→ 執行整月分析（約 15 秒）→ 看折線圖、數據分析卡片，再按「下載分析報告（PDF）」，**打開 PDF 確認中文、表格和換行沒有跑版**
   - 「情境比較」→ 執行情境試算（約 15 秒）→ 看每年省多少錢和回收年限是否合理（預設投資金額是估計值，請依實際報價調整）
   - 「異常偵測」→ 用示範合成資料，換不同通道與門檻看看
6. **手機版**：在瀏覽器按 F12 → 裝置模式選 iPhone，看首頁卡片是否變成兩欄、頁首標籤能不能橫向滑動。
7. **網頁版登入**：設定 `HEMS_REQUIRE_LOGIN=1` 再啟動 → 登入畫面下方的「👀 以示範帳號試用」應該直接進入「示範：四口之家（標配）」。
8. 確認沒問題後再合併到 main，push 之後到 GitHub 的 Actions 分頁看第一次 CI 是否通過（README 徽章會顯示狀態）。

# 程式文件（開發者指南）

這份文件說明程式怎麼組成、資料怎麼流動，以及要加功能時該改哪裡。
專案介紹與成果請看 [README](../README.md)；每個模組開頭的 docstring 有更細的說明。

## 1. 執行流程

```
app.py（每次使用者操作都會從頭執行一次）
 ├─ ensure_session()          ui/account.py：登入、載入最後使用的設定檔（session_state.settings / ui）
 ├─ get_analyzer(sim_key)     ui/context.py：依設定組裝整個系統（快取：設定沒變就不重建）
 │    └─ build_analyzer()     ui/settings.py：House → Schedule → LoadModel → Simulator → Optimizer → Analyzer
 ├─ 左側選單（設定視窗）       修改 session_state.ui / settings，按「套用」後 st.rerun()
 ├─ run_schedule()            所選模式最佳化 → ControlPlan + SimulationResult（快取）
 ├─ AppContext                各頁共用的設定、日期、推演結果
 ├─ st.navigation(...).run()  執行目前頁面的 render(ctx)（包在 guarded() 裡，頁面出錯不會整個當掉）
 └─ autosave(store)           設定或介面選項有變動就寫回資料庫
```

**快取規則**：設定字典序列化成 JSON 字串（`sim_key`，排除平面圖位置）當 key。
昂貴的計算都在 `ui/context.py` 用 `st.cache_data` / `st.cache_resource` 包起來，參數名稱前加底線（`_analyzer`）
代表不參與 key 計算，所以一定要另外傳 `settings_key`。

## 2. 模組地圖

| 層 | 模組 | 職責 |
|---|---|---|
| 輸入 | `core/models.py` | 空間範本、家電、房屋、成員的資料結構；`house_from_config()` 由範本組房屋 |
| 輸入 | `core/data_source.py` | 資料來源介面：天氣（中央氣象署 CSV／Open-Meteo 任一地點）、電表（CSV）；模擬與感測器走同一介面 |
| 輸入 | `core/tariff.py` | 台電電價：時段、費率、離峰日、月電費（時間電價、累進） |
| 模擬 | `core/schedule.py` | 作息 → 每個空間每小時的人數與活動 |
| 模擬 | `core/load_model.py` | 冷房負載、冷氣性能曲線、家電耗電、室溫與室內濕度、隔間熱交換；與設定溫度無關的部分快取（`_prepare`） |
| 模擬 | `core/solar.py` | 太陽位置、直射／散射分離、各方位牆面日射 |
| 模擬 | `core/simulator.py` | 24 小時推演：`ControlPlan` → `SimulationResult`；`run_spaces()` 隔間熱交換迭代；`run_month()` |
| 效能 | `core/parallel.py` | 多核心平行（整月分析、整月推演每天一個工作）；`HEMS_WORKERS=1` 關閉 |
| 最佳化 | `core/optimizer.py` | 四種模式（modes.json 參數）的最佳化 |
| 分析 | `core/metrics.py` | 指標、PMV/PPD、模式比較、節省建議、校正（ASHRAE Guideline 14） |
| 資料 | `core/storage.py` | SQLAlchemy：帳號、設定檔、家電使用紀錄 |
| 介面 | `ui/context.py` | 各頁共用資料（AppContext）與所有快取 |
| 介面 | `ui/settings.py` | 設定字典 ↔ 核心模組（房型表格、作息時間） |
| 介面 | `ui/usage.py` | 家電使用紀錄、本月／下月預測、帳單比對 |
| 介面 | `ui/insights.py` | 整月每日資料、數據分析式建議 |
| 介面 | `ui/whatif.py` | 情境比較（換設備、改習慣）與投資回收 |
| 介面 | `ui/anomaly.py` | 用電異常偵測（實測 − 模擬的穩健統計門檻） |
| 介面 | `ui/pdf_report.py` | 整月分析報告 PDF |
| 介面 | `ui/wizard.py` | 新手引導精靈 |
| 介面 | `ui/logs.py` | 錯誤記錄與頁面錯誤處理 |
| 介面 | `ui/demo.py` | 示範帳號（登入畫面免註冊試用，每次進入重設）與示範設定檔 |
| 介面 | `ui/floorplan.py` + `floorplan_frontend/` | 智慧平面圖自訂元件（拖曳、遙控器、一天動畫播放） |
| 介面 | `ui/pages/*.py` | 各頁面，每頁一個 `render(ctx)` |

**分工原則**：`core/` 只放計算（不 import streamlit），介面需要的新資料在 `ui/` 整理；
介面只修改「設定字典」，不寫回 `config/` 的 JSON，預設值永遠保留。

## 3. 設定字典（settings）

```python
{
  "house": {...},             # config/default_house.json：空間（範本、坪數、家電增減）
  "appliance_edits": {...},   # "空間id/家電id" → {"power_w", "enabled"}
  "schedule": {...},          # config/schedule.json：成員、作息範本
  "modes": {...},             # config/modes.json：四種模式參數、舒適偏好
  "baseline_setpoint_c": 26,  # 一般習慣的冷氣溫度
  "layout": {...},            # 平面圖房間與家電位置（不影響計算，sim_key 排除）
}
```
介面選項（日期、時間、電價方案、模式、是否完成新手引導…）存在 `session_state.ui`，預設值在 `app.py` 的 `DEFAULT_UI`。
兩者都隨設定檔自動存進資料庫。

## 4. 常見修改

**新增頁面**：在 `ui/pages/` 新增模組並實作 `render(ctx: AppContext)`，加到 `app.py` 的 `PAGES`，
再到 `tests/test_app.py` 的 `test_every_page_renders` 參數加上頁面名稱。

**新增分析分頁**：在 `ui/pages/analysis.py` 的 `st.tabs([...])` 加名稱與對應函式；需要較久的計算放在
`ui/context.py` 並加上 `st.cache_data`。

**新增情境（What-if）**：在 `ui/whatif.py` 的 `SCENARIOS` 加一筆 `Scenario`，並在 `apply_scenario()`
寫出這個情境要改的設定或負載模型參數（不改 core 的演算法）。

**接上真實感測器**：實作 `core/data_source.py` 的 `MeterSource`（`get_hourly_consumption(day)` 回傳
hour、space_id、appliance_id、kwh），或把資料匯出成 `data/meter/README.md` 的 CSV 格式放進 `data/meter/`，
「分析 → 異常偵測」就會出現在選單中。

**新增資料表**：在 `core/storage.py` 加 SQLAlchemy 模型（`Base.metadata.create_all` 會自動建立新表），
並在 `Store` 加存取方法；每個方法都要檢查設定檔屬於目前使用者。

## 5. 錯誤處理與記錄

- 記錄檔：`data/user/logs/hems.log`（1 MB × 3 份輪替，不進 git）；環境變數 `HEMS_LOG_DIR` 可改位置。
- 每個頁面都包在 `ui/logs.guarded()` 裡：未預期的錯誤寫入完整 traceback，畫面顯示錯誤訊息與「技術細節」。
- 設定錯誤（例如刪掉成員正在使用的臥室）在 `app.py` 顯示「設定有誤」與「恢復預設設定」按鈕。
- 自動存檔失敗（資料庫暫時無法寫入）只顯示提示，下次操作會再試一次。
- 記錄登入、註冊、設定檔載入／刪除、匯入失敗等事件；**不記錄密碼**。

## 6. 測試與程式碼檢查

```bash
python -m pytest -q -p no:warnings     # 全部測試（含 Streamlit AppTest 實際執行各頁面）
python -m ruff check .                 # 程式碼檢查（設定在 ruff.toml）
```
- `tests/conftest.py` 讓測試使用暫存的資料庫與記錄資料夾，不會動到使用者的資料。
- GitHub Actions（`.github/workflows/tests.yml`）在 push 到 main 與 pull request 時自動執行以上兩項。
- AppTest 不會像瀏覽器一樣只重跑對話框（fragment），所以對話框用 session 旗標保持開啟（見 `ui/wizard.py`）。

## 7. 指令列工具

| 指令 | 用途 |
|---|---|
| `python -m core.simulator [日期]` | 推演標配房型（省略日期時跑夏、冬代表日），輸出 CSV、PNG 到 output/ |
| `python -m core.metrics --month 2026-07` | 整月分析與節省建議報告（Markdown，約 3 秒） |
| `python -m core.metrics --calibrate data/meter/demo_synthetic_2026-07.csv` | 模型校正（ASHRAE Guideline 14） |
| `python scripts/ml_forecast.py` | B1b 混合用電預測模型比較（報告與圖輸出到 docs/） |
| `python scripts/fetch_taipower_open_data.py` | 下載台電公開用電資料 |

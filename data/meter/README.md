# 用電實測資料（智慧插座、電表）

本資料夾放置實測用電 CSV，由 `core.data_source.CsvMeterSource` 讀取，與模擬結果走同一個介面（`MeterSource`）。

## 格式

| 欄位 | 說明 |
|---|---|
| timestamp | ISO 時間，例如 `2026-07-20T13:15:00`（取樣間隔不限） |
| space_id | 空間 id（與 `config/default_house.json` 相同） |
| appliance_id | 家電 id；總電表填 `main` |
| power_w 或 kwh | 瞬時功率（W）或該筆期間用電量（kWh），二擇一 |

## 檔案

- `demo_synthetic_2026-07.csv`：**示範用合成資料，不是實測資料**。由 `scripts/make_demo_meter_data.py`
  以模擬結果加上刻意的差異產生（冷氣 +15%、熱水器時間飄移、30 W 待機、±10% 波動），
  用來展示 ASHRAE Guideline 14 模型校正流程：`python -m core.metrics --calibrate data/meter/demo_synthetic_2026-07.csv`

- `demo_ami_2026-07.csv`：**示範用合成資料，不是實測資料**。台電智慧電表（AMI）樣式的全屋每 15 分鐘用電
  （時間標在區間結束），由 `core.calibration.demo_ami_csv` 以模擬值加上冷氣 ×1.25、其他家電 ×0.9、
  60 W 待機與 ±8% 波動產生；給「分析 → 電表校正」示範自動校正能找回這些差異。
  電表校正可讀任意欄名的 CSV（時間＋用電度數或功率），不必符合上表格式。

取得真實智慧插座或電表資料後，依上表格式匯出放在這裡即可。

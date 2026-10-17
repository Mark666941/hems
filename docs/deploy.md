# 部署到網路（階段 8）：Streamlit Community Cloud ＋ Supabase

完成後會得到一個網址（例如 `https://hems-xxx.streamlit.app`），任何人用瀏覽器就能打開：
註冊帳號、或按「👀 以示範帳號試用（免註冊）」直接體驗。每個人的設定存在雲端資料庫，換電腦登入還在。

全程免費，大約 10–15 分鐘。需要兩個帳號：**Supabase**（資料庫）與 **Streamlit Community Cloud**（主機），
兩者都可以直接用 GitHub 帳號登入。

---

## 步驟 1：建立 Supabase 資料庫（約 5 分鐘）

1. 打開 <https://supabase.com> → **Start your project** → 用 **GitHub** 登入。
2. **New project**：
   - Name：`hems`
   - Database Password：按 **Generate a password**，**把密碼複製存好**（等一下要用，之後看不到）
   - Region：**Northeast Asia (Tokyo)** 或 **Southeast Asia (Singapore)**（離台灣近）
   - 方案選 **Free** → **Create new project**，等約 2 分鐘建立完成
3. 專案首頁上方按 **Connect**（或 Project Settings → Database → Connection string）：
   - 選 **Session pooler**（⚠️ 不要選 Direct connection：Streamlit Cloud 不支援 IPv6，直接連線會失敗）
   - 複製連線字串，長得像：
     `postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres`
4. 把連線字串改兩個地方：
   - 開頭 `postgresql://` 改成 `postgresql+pg8000://`
   - `[YOUR-PASSWORD]` 換成第 2 步存的密碼（密碼若含 `@ : / ?` 等符號，要先做網址編碼，例如 `@` → `%40`）

資料表不用手動建立：程式第一次連線時會自動建立 `users`、`profiles`、`usage_records`。

## 步驟 2：部署到 Streamlit Community Cloud（約 5 分鐘）

1. 打開 <https://share.streamlit.io> → 用 **GitHub** 登入，第一次會要求授權讀取 repository，按同意。
2. 右上角 **Create app** → **Deploy a public app from GitHub**：
   - Repository：`Mark666941/hems`
   - Branch：`main`
   - Main file path：`app.py`
   - App URL：可自訂，例如 `hems-mark`（網址會是 `hems-mark.streamlit.app`）
3. 按 **Advanced settings**：
   - Python version：**3.12**
   - Secrets：貼上下面內容（換成步驟 1 改好的連線字串）

     ```toml
     [database]
     url = "postgresql+pg8000://postgres.abcdefghijkl:你的密碼@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
     ```
4. 按 **Deploy**。第一次會安裝套件（約 3–5 分鐘），右下角 **Manage app** 可以看安裝進度與錯誤訊息。

## 步驟 3：確認

- 打開網址 → 看到「登入」畫面（雲端版一定要登入）
- 按 **👀 以示範帳號試用（免註冊）** → 進入「示範：四口之家」→ 首頁、平面圖、分析都能操作
- 註冊一個自己的帳號 → 修改設定 → 重新整理網頁或換電腦登入，設定仍在
- 「分析 → 整月分析與建議 → 下載分析報告（PDF）」：中文正常（`packages.txt` 會安裝 Noto CJK 字型）

## 步驟 4（建議）：自動保持資料庫活躍

Supabase 免費方案 7 天沒有資料庫活動會暫停，網頁版就無法登入。`.github/workflows/keepalive.yml` 每 3 天自動連一次資料庫：

1. GitHub 的 repository 頁面 → **Settings** → 左側 **Secrets and variables** → **Actions**
2. **New repository secret**：Name 填 `HEMS_DATABASE_URL`，Secret 貼上和 Streamlit Secrets 相同的網址（只要網址本身，不含 `url =` 和引號）
3. 上方 **Actions** 分頁 → 左側 **keepalive** → **Run workflow**，看到綠色勾勾、紀錄裡有「資料庫連線正常」就完成
4. 之後更換資料庫密碼時，Streamlit Secrets 與這個 GitHub Secret 都要一起改

## 常見問題

| 狀況 | 原因與解法 |
|---|---|
| Manage app 顯示 `connection refused`、`timeout`、`Network is unreachable` | 用了 Direct connection。改用 **Session pooler** 的連線字串 |
| `password authentication failed` | 密碼錯誤或含特殊符號沒有網址編碼；可在 Supabase 重設資料庫密碼 |
| `No module named 'pg8000'` | 連線字串開頭沒有改成 `postgresql+pg8000://` |
| 一段時間沒人使用後網址顯示「This app has gone to sleep」 | 免費方案閒置會休眠，按 **Yes, get this app back up!** 約 30 秒恢復 |
| 想更新網站 | 程式 push 到 GitHub main 後，Streamlit Cloud 會自動重新部署 |

## 技術說明（面試用）

- **同一套程式兩種部署**：資料層用 SQLAlchemy，桌面版連本機 SQLite（免登入），網頁版從 Streamlit secrets
  讀 PostgreSQL 網址（強制登入）；`core/storage.py` 不需要改任何程式
- **連線加密**：pg8000 以 SSL 連線（`HEMS_DB_SSL`，預設 require；可改 verify 並提供 Supabase 根憑證）
- **雲端資源**：免費主機約 1 GB 記憶體。多核心平行計算依容器實際的 CPU 配額與記憶體上限決定子程序數
  （`core/parallel.py`），避免照實體主機核心數開太多程序而記憶體不足
- **密碼**：PBKDF2-SHA256 加鹽雜湊後儲存，不存明碼

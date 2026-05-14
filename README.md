# Firstrade Transaction Analyzer

Firstrade Transaction Analyzer 是一個以 Firstrade 交易紀錄為核心的投資績效分析儀表板。第一版目標是把使用者的交易 CSV 轉成可驗算的投資分析資料，並透過 GitHub Pages 呈現成具有金融專業感與 cyberpunk 視覺風格的單頁網站。

目前網站入口：

```text
https://bcjack0125.github.io/firstrade-transaction-analyzer/
```

本機預覽：

```bash
python -m http.server 8000 --directory docs
```

開啟：

```text
http://localhost:8000
```

## 第一版功能

- 整體績效分析：Total PnL、Total Realized PnL、Total Unrealized PnL、目前報酬率、Win Rate、Profit Factor、Health Score、Max Drawdown。
- 投入成本計算：從 `transactions.csv` 中抓取 `Wire Funds Received` 存款與 `rebate for wire` 回饋。
- 風險調整報酬：計算 Sharpe Ratio，並與 3 個月期美國國庫券殖利率比較。
- 個股分析：依股票代號彙總 realized / unrealized / total PnL、持有狀態、數量、成本、市值、勝率、最近交易日。
- 個股排序與篩選：可切換全部、現持有、已結清，並依最近交易、最舊交易、盈虧高低、代號排序。
- 市價來源追蹤：現持有股票優先使用 yfinance，失敗時 fallback 到 positions Excel，再失敗才使用最後交易價。
- 指標驗算：內建多項 audit checks，確認前端顯示資訊與分析邏輯一致。
- Positions 參考驗算：`data/67744964-positions.xlsx` 作為現倉數量與成本驗證參考，不直接覆蓋主要分析。
- 分析建議：根據報酬率、Realized/Unrealized 結構、勝率、Profit Factor、Sharpe、融資曝險與主要浮虧來源產生稱讚或提醒。

## 專案結構

```text
.
├── .github/workflows/analyze.yml   # GitHub Actions 分析與輸出同步流程
├── data/
│   ├── transactions.csv            # 主要交易資料來源
│   ├── 67744964-positions.xlsx     # 現倉參考驗算檔，不作為主資料來源
│   ├── output.json                 # 分析輸出
│   └── report.html                 # 簡易 HTML 報告
├── docs/
│   ├── index.html                  # GitHub Pages 前端
│   └── output.json                 # GitHub Pages 使用的分析資料
├── scripts/
│   ├── analyze.py                  # 主分析流程
│   ├── fifo.py                     # FIFO 損益計算
│   └── health.py                   # 健康度指標
└── README.md
```

## 資料來源

### `data/transactions.csv`

這是主要資料來源。第一版預期欄位如下：

| 欄位 | 說明 |
| --- | --- |
| `日期` | 交易日期 |
| `交易類別` | 例如 `買進`、`賣出`、`存款`、`其他` |
| `數量` | 股數 |
| `說明` | Firstrade 交易描述 |
| `代號` | 股票或 ETF 代號 |
| `賬戶類別` | 例如 `現金`、`融資` |
| `價格` | 成交價格 |
| `金額` | 現金流金額 |

### `data/67744964-positions.xlsx`

這個檔案作為「驗證參考」，不是主分析資料來源。主要用途：

- 檢查 FIFO 算出的現倉數量是否與券商現倉一致。
- 檢查 FIFO 算出的持倉成本是否與券商成本接近。
- 當 yfinance 無法取得現價時，作為價格 fallback。

## 分析邏輯

### FIFO Realized PnL

`scripts/fifo.py` 會依日期排序交易，使用 FIFO 計算已實現損益。

- `買進` 建立庫存 lot。
- `賣出` 依最早 lot 沖銷並產生 realized PnL。
- 若遇到缺少庫存的賣出，會以負庫存 lot 表示 short 或資料不足狀態。

### Unrealized PnL

現持有部位的 unrealized PnL 計算：

```text
unrealized_pnl = (market_price - lot_cost) * quantity
```

價格來源順序：

1. yfinance 最近收盤價。
2. `positions.xlsx` 的 `價格` 欄。
3. `transactions.csv` 中該代號最後交易價。

每個個股會輸出 `price_source`，方便判斷目前採用哪個價格來源。

### 投入成本與目前報酬率

投入成本只包含下列兩類現金流：

- `交易類別 = 存款` 且 `說明` 包含 `Wire Funds Received`
- `交易類別 = 其他` 且 `說明` 包含 `rebate for wire`

比對大小寫不敏感。

```text
return_pct = total_pnl / invested_cost
```

### Sharpe Ratio

Sharpe Ratio 使用每日 realized PnL 除以投入成本後的日報酬序列估算：

```text
daily_return = daily_realized_pnl / invested_cost
sharpe = mean(daily_return - daily_risk_free) / std(daily_return) * sqrt(252)
```

風險自由利率來源：

- yfinance `^IRX`，代表 13-week Treasury Bill。
- 若 yfinance 失敗，使用 fallback 5%，並在 JSON 中標記來源。

### Health Score

目前第一版 Health Score 是簡化模型：

```text
profit_factor = average_win / abs(average_loss)
health_score = clamp(50 + profit_factor * 10, 0, 100)
```

此指標用來快速描述交易損益品質，不等同於完整投資風險評級。

### 指標驗算

`metric_audit` 會驗算：

- Total PnL = sum(realized) + sum(unrealized)
- Win Rate = winning realized trades / realized trades
- Profit Factor = average win / abs(average loss)
- Health Score 公式一致性
- Daily realized sum 與時間序列累計值
- Reconciliation delta
- Asset Value = sum(total_by_account)
- Allocation ratios 加總

`positions_reference` 會檢查：

- 計算持倉數量 vs positions 參考數量
- 計算持倉成本 vs positions 參考成本

## 輸出資料

主分析會產生：

```text
data/output.json
docs/output.json
data/report.html
```

`docs/index.html` 會讀取：

```text
./output.json?v=<timestamp>
```

加上 timestamp 是為了避免 GitHub Pages 或瀏覽器快取造成新前端讀到舊 JSON。

## 本機執行

建議使用虛擬環境：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install pandas numpy openpyxl yfinance
```

執行分析：

```bash
.venv\Scripts\python -B scripts\analyze.py
```

同步到 GitHub Pages 資料目錄：

```bash
copy data\output.json docs\output.json
```

啟動本機網站：

```bash
python -m http.server 8000 --directory docs
```

## GitHub Actions

`.github/workflows/analyze.yml` 會在以下情況執行：

- 手動觸發 `workflow_dispatch`
- push 修改：
  - `data/transactions.csv`
  - `data/*-positions.xlsx`
  - `scripts/*.py`
  - `.github/workflows/analyze.yml`

流程會：

1. 安裝 Python 3.12。
2. 安裝 `pandas numpy openpyxl yfinance`。
3. 執行 `python scripts/analyze.py`。
4. 將 `data/output.json` 複製到 `docs/output.json`。
5. 自動 commit 分析結果。

## LLM 投資健檢 (可選)

分析流程支援在 GitHub Actions 內呼叫 LLM，將投資健檢結果寫入 `output.json` 並顯示於前端。

### 必要 Secrets

在 GitHub repo 的 Settings → Secrets and variables → Actions → Secrets 新增：

- `LLM_API_KEY`：你的 API key
- `LLM_API_URL`：API endpoint（例如 Gemini 的 `https://generativelanguage.googleapis.com/v1beta/models/gemma-4-26b-a4b-it:generateContent`）
- `LLM_MODEL`：模型名稱（OpenAI-compatible 需要；Gemini REST 可省略）

### 可選 Secrets

- `LLM_TEMPERATURE`（預設 0.2）
- `LLM_MAX_TOKENS`（預設 900）
- `LLM_TIMEOUT`（預設 45 秒）
- `LLM_HTTP_REFERER`（部分供應商需要）
- `LLM_APP_TITLE`（部分供應商需要）

若不設定 LLM 參數，流程會自動跳過，不會使分析失敗。

## GitHub Pages 設定

建議 GitHub Pages 設定：

```text
Source: Deploy from a branch
Branch: main
Folder: /docs
```

網站會使用：

```text
docs/index.html
docs/output.json
```

## 驗證指令

檢查 Python 語法：

```bash
python -m py_compile scripts\analyze.py scripts\fifo.py scripts\health.py
```

檢查 JSON：

```bash
python -m json.tool docs\output.json
```

檢查前端 script 語法：

```bash
node -e "const fs=require('fs'); const html=fs.readFileSync('docs/index.html','utf8'); const js=html.match(/<script>([\s\S]*)<\/script>/)[1]; new Function(js); console.log('script ok')"
```

## 第一版限制

- Sharpe Ratio 目前以 realized PnL 的日序列估算，尚未使用完整每日資產淨值序列。
- yfinance 價格可能因市場休市、代號查無資料或網路問題失敗，因此保留 fallback。
- `positions.xlsx` 僅作為參考驗算，不作為主要持倉真相來源。
- 手續費、稅務、匯率、股利、拆股等事件尚未完整建模。
- FIFO 模型會盡量處理 short 或缺失庫存，但若原始交易紀錄不完整，仍可能需要人工檢查。

## Roadmap

- 支援手續費與股利。
- 增加每日 NAV 與 time-weighted return。
- 增加 benchmark，例如 SPY / QQQ 對比。
- 增加個股風險集中度、產業集中度與槓桿警示。
- 讓 Positions 檔案名稱可設定，不綁定單一帳號檔名。

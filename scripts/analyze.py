"""Analyze Firstrade transactions using FIFO and basic health checks."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from fifo import calculate_fifo_pnl
from health import calculate_health


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CSV_PATH = DATA_DIR / "transactions.csv"
JSON_PATH = DATA_DIR / "output.json"
REPORT_PATH = DATA_DIR / "report.html"


def _get_column(df: pd.DataFrame, candidates: list[str]) -> str:
    for name in candidates:
        if name in df.columns:
            return name
    raise ValueError(f"Missing required column. Tried: {', '.join(candidates)}")


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    column_aliases = {
        "日期": "date",
        "交易類別": "action",
        "數量": "qty",
        "代號": "symbol",
        "價格": "price",
        "賬戶類別": "account_type",
        "說明": "description",
        "金額": "amount",
    }

    rename_map = {}
    for c in df.columns:
        stripped = c.strip()
        lowered = stripped.lower()
        if stripped in column_aliases:
            rename_map[c] = column_aliases[stripped]
        elif lowered in column_aliases:
            rename_map[c] = column_aliases[lowered]
        else:
            rename_map[c] = lowered

    return df.rename(columns=rename_map)


def _to_float(value: object) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text == "":
        return 0.0
    return float(text)


def _mock_prices(df: pd.DataFrame) -> dict:
    symbol_col = _get_column(df, ["symbol", "ticker"])
    price_col = _get_column(df, ["price", "trade_price", "avg_price"])
    action_col = _get_column(df, ["action", "side", "type"])
    action_map = {
        "買進": "buy",
        "賣出": "sell",
        "buy": "buy",
        "sell": "sell",
    }
    prices = {}
    for _, row in df.iterrows():
        symbol = str(row[symbol_col]).strip().upper()
        if not symbol or symbol.lower() == "nan":
            continue
        action_raw = str(row[action_col]).strip()
        action = action_map.get(action_raw, action_raw.lower())
        if not (action.startswith("b") or action.startswith("s")):
            continue
        price = _to_float(row[price_col])
        if price <= 0:
            continue
        prices[symbol] = price
    return prices


def main() -> None:
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Missing CSV: {CSV_PATH}")

    df = pd.read_csv(CSV_PATH)
    df = _normalize_columns(df)

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["_row_order"] = range(len(df))
        df = df.sort_values(["date", "_row_order"], kind="mergesort")
        df = df.drop(columns=["_row_order"])
    else:
        df = df.sort_index()

    realized, inventory = calculate_fifo_pnl(df)

    prices = _mock_prices(df)

    unrealized = []
    for stock, lots in inventory.items():
        for lot in lots:
            price = prices.get(stock, 100.0)
            pnl = (price - float(lot["price"])) * float(lot["qty"])
            unrealized.append(pnl)

    health = calculate_health(realized, unrealized)

    output = {
        "realized": realized,
        "unrealized": unrealized,
        "health": health,
    }

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

    REPORT_PATH.write_text(
        """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Firstrade Transaction Report</title>
  </head>
  <body>
    <main>
      <h1>Firstrade Transaction Report</h1>
      <p>Health score: {score}</p>
      <p>Total PnL: {total}</p>
    </main>
  </body>
</html>
""".format(
            score=health["score"],
            total=health["total"],
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

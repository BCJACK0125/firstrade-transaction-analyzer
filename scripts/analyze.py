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


def _mock_prices(df: pd.DataFrame) -> dict:
    symbol_col = _get_column(df, ["symbol", "ticker"])
    price_col = _get_column(df, ["price", "trade_price", "avg_price"])
    prices = {}
    for _, row in df.iterrows():
        symbol = str(row[symbol_col]).strip().upper()
        prices[symbol] = float(row[price_col])
    return prices


def main() -> None:
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Missing CSV: {CSV_PATH}")

    df = pd.read_csv(CSV_PATH)
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date")
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

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


def _compute_reconciliation(
    df: pd.DataFrame, realized: list[dict], inventory: dict[str, list[dict]]
) -> dict:
    try:
        action_col = _get_column(df, ["action", "side", "type"])
        amount_col = _get_column(df, ["amount", "total", "cashflow"])
    except ValueError as exc:
        return {"enabled": False, "reason": str(exc)}

    action_map = {
        "買進": "buy",
        "賣出": "sell",
        "buy": "buy",
        "sell": "sell",
    }

    total_buy = 0.0
    total_sell = 0.0

    for _, row in df.iterrows():
        action_raw = str(row[action_col]).strip()
        action = action_map.get(action_raw, action_raw.lower())
        if action.startswith("b"):
            total_buy += _to_float(row[amount_col])
        elif action.startswith("s"):
            total_sell += _to_float(row[amount_col])

    net_cashflow = total_buy + total_sell

    remaining_cost_basis = 0.0
    for lots in inventory.values():
        for lot in lots:
            remaining_cost_basis += float(lot["price"]) * float(lot["qty"])

    realized_total = sum(float(item["pnl"]) for item in realized)
    expected_realized = net_cashflow + remaining_cost_basis
    delta = realized_total - expected_realized

    return {
        "enabled": True,
        "total_buy_amount": total_buy,
        "total_sell_amount": total_sell,
        "net_cashflow": net_cashflow,
        "remaining_cost_basis": remaining_cost_basis,
        "realized_total": realized_total,
        "expected_realized": expected_realized,
        "delta": delta,
    }


def _build_timeseries(realized: list[dict]) -> dict:
    if not realized:
        return {
            "daily": [],
            "weekly": [],
            "monthly": [],
            "max_drawdown": {"daily": 0.0, "weekly": 0.0, "monthly": 0.0},
        }

    df = pd.DataFrame(realized)
    if "realized_date" not in df.columns:
        df["realized_date"] = df.get("sell_date")
        if "side" in df.columns:
            df.loc[df["side"] == "short", "realized_date"] = df.get("buy_date")

    df["realized_date"] = pd.to_datetime(df["realized_date"], errors="coerce")
    df = df.dropna(subset=["realized_date"])
    if df.empty:
        return {
            "daily": [],
            "weekly": [],
            "monthly": [],
            "max_drawdown": {"daily": 0.0, "weekly": 0.0, "monthly": 0.0},
        }

    if "account_type" not in df.columns:
        df["account_type"] = "unknown"
    else:
        df["account_type"] = df["account_type"].fillna("unknown")

    def _aggregate(freq: str) -> tuple[list[dict], float]:
        period = df["realized_date"].dt.to_period(freq).dt.to_timestamp()
        grouped = (
            df.assign(period=period)
            .groupby(["period", "account_type"], dropna=False)["pnl"]
            .sum()
            .reset_index()
        )

        pivot = grouped.pivot(index="period", columns="account_type", values="pnl").fillna(0.0)
        pivot = pivot.sort_index()
        pivot["total"] = pivot.sum(axis=1)
        pivot["cumulative_total"] = pivot["total"].cumsum()
        pivot["drawdown"] = pivot["cumulative_total"] - pivot["cumulative_total"].cummax()

        rows: list[dict] = []
        account_cols = [c for c in pivot.columns if c not in ["total", "cumulative_total", "drawdown"]]

        for idx, row in pivot.iterrows():
            by_account = {c: float(row[c]) for c in account_cols}
            rows.append(
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "total": float(row["total"]),
                    "by_account": by_account,
                    "cumulative_total": float(row["cumulative_total"]),
                    "drawdown": float(row["drawdown"]),
                }
            )

        max_drawdown = float(pivot["drawdown"].min()) if not pivot.empty else 0.0
        return rows, max_drawdown

    daily, dd_daily = _aggregate("D")
    weekly, dd_weekly = _aggregate("W")
    monthly, dd_monthly = _aggregate("M")

    return {
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "max_drawdown": {
            "daily": dd_daily,
            "weekly": dd_weekly,
            "monthly": dd_monthly,
        },
    }


def _compute_invested_cost(df: pd.DataFrame) -> dict:
    try:
        action_col = _get_column(df, ["action", "side", "type"])
        desc_col = _get_column(df, ["description", "desc", "memo"])
        amount_col = _get_column(df, ["amount", "total", "cashflow"])
    except ValueError as exc:
        return {"enabled": False, "reason": str(exc)}

    try:
        account_col = _get_column(df, ["account_type", "account", "accounttype"])
    except ValueError:
        account_col = None

    total = 0.0
    by_account: dict[str, float] = {}

    for _, row in df.iterrows():
        action_raw = str(row[action_col]).strip()
        desc_norm = " ".join(str(row[desc_col]).split()).casefold()
        amount = _to_float(row[amount_col])

        is_deposit = action_raw == "存款" and "wire funds received" in desc_norm
        is_rebate = action_raw == "其他" and "rebate for wire" in desc_norm

        if not (is_deposit or is_rebate):
            continue

        total += amount
        account = "unknown"
        if account_col:
            account = str(row[account_col]).strip() or "unknown"
        by_account[account] = by_account.get(account, 0.0) + amount

    return {"enabled": True, "total": total, "by_account": by_account}


def _compute_asset_value(
    df: pd.DataFrame, inventory: dict[str, list[dict]], prices: dict
) -> dict:
    try:
        amount_col = _get_column(df, ["amount", "total", "cashflow"])
    except ValueError as exc:
        return {"enabled": False, "reason": str(exc)}

    try:
        account_col = _get_column(df, ["account_type", "account", "accounttype"])
    except ValueError:
        account_col = None

    cash_by_account: dict[str, float] = {}
    for _, row in df.iterrows():
        account = "unknown"
        if account_col:
            account = str(row[account_col]).strip() or "unknown"
        cash_by_account[account] = cash_by_account.get(account, 0.0) + _to_float(row[amount_col])

    holdings_by_account: dict[str, float] = {}
    for symbol, lots in inventory.items():
        price = prices.get(symbol, 100.0)
        for lot in lots:
            account = str(lot.get("account_type", "unknown") or "unknown")
            holdings_by_account[account] = holdings_by_account.get(account, 0.0) + (
                float(lot["qty"]) * float(price)
            )

    total_by_account: dict[str, float] = {}
    accounts = set(cash_by_account) | set(holdings_by_account)
    for account in accounts:
        total_by_account[account] = cash_by_account.get(account, 0.0) + holdings_by_account.get(
            account, 0.0
        )

    total = sum(total_by_account.values())

    return {
        "enabled": True,
        "cash_by_account": cash_by_account,
        "holdings_by_account": holdings_by_account,
        "total_by_account": total_by_account,
        "total": total,
    }


def _compute_asset_allocation(asset_value: dict) -> dict:
    if not asset_value.get("enabled"):
        return {"enabled": False, "reason": asset_value.get("reason", "asset_value disabled")}

    cash_by_account = asset_value.get("cash_by_account", {})
    holdings_by_account = asset_value.get("holdings_by_account", {})

    cash_balance = float(cash_by_account.get("現金", 0.0))
    cash_stock = float(holdings_by_account.get("現金", 0.0))
    margin_stock = float(holdings_by_account.get("融資", 0.0))
    total = float(asset_value.get("total", 0.0))

    other = total - (cash_balance + cash_stock + margin_stock)

    ratios = {}
    if total != 0:
        ratios = {
            "cash_balance": cash_balance / total,
            "cash_stock": cash_stock / total,
            "margin_stock": margin_stock / total,
            "other": other / total,
        }

    return {
        "enabled": True,
        "cash_balance": cash_balance,
        "cash_stock": cash_stock,
        "margin_stock": margin_stock,
        "other": other,
        "total": total,
        "ratios": ratios,
    }


def _compute_pnl_by_account(realized: list[dict], unrealized_by_account: dict) -> dict:
    realized_by_account: dict[str, float] = {}
    for item in realized:
        account = str(item.get("account_type", "unknown") or "unknown")
        realized_by_account[account] = realized_by_account.get(account, 0.0) + float(item["pnl"])

    total_by_account: dict[str, float] = {}
    accounts = set(realized_by_account) | set(unrealized_by_account)
    for account in accounts:
        total_by_account[account] = realized_by_account.get(account, 0.0) + float(
            unrealized_by_account.get(account, 0.0)
        )

    total = sum(total_by_account.values())

    return {
        "enabled": True,
        "realized": realized_by_account,
        "unrealized": unrealized_by_account,
        "total": total_by_account,
        "total_all": total,
    }


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
    unrealized_by_account: dict[str, float] = {}
    for stock, lots in inventory.items():
        for lot in lots:
            price = prices.get(stock, 100.0)
            pnl = (price - float(lot["price"])) * float(lot["qty"])
            unrealized.append(pnl)
            account = str(lot.get("account_type", "unknown") or "unknown")
            unrealized_by_account[account] = unrealized_by_account.get(account, 0.0) + pnl

    health = calculate_health(realized, unrealized)
    reconciliation = _compute_reconciliation(df, realized, inventory)
    timeseries = _build_timeseries(realized)
    invested_cost = _compute_invested_cost(df)
    asset_value = _compute_asset_value(df, inventory, prices)
    asset_allocation = _compute_asset_allocation(asset_value)
    pnl_by_account = _compute_pnl_by_account(realized, unrealized_by_account)

    output = {
        "realized": realized,
        "unrealized": unrealized,
        "health": health,
        "reconciliation": reconciliation,
        "timeseries": timeseries,
        "invested_cost": invested_cost,
        "asset_value": asset_value,
        "asset_allocation": asset_allocation,
        "pnl_by_account": pnl_by_account,
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

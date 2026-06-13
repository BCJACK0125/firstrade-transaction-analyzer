"""Analyze Firstrade transactions using FIFO and basic health checks."""

from __future__ import annotations

import json
import math
import os
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from urllib import error, request
from pathlib import Path

import numpy as np
import pandas as pd

from fifo import calculate_fifo_pnl
from health import calculate_health


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CSV_PATH = DATA_DIR / "transactions.csv"
JSON_PATH = DATA_DIR / "output.json"
REPORT_PATH = DATA_DIR / "report.html"
POSITIONS_PATH = DATA_DIR / "67744964-positions.xlsx"
TRADING_DAYS = 252
DEFAULT_RISK_FREE_RATE = 0.05
GITHUB_MODELS_API_URL = "https://models.github.ai/inference/chat/completions"
DEFAULT_GITHUB_MODEL = "openai/gpt-4o-mini"


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


def _clean_records(value: object) -> object:
    if isinstance(value, dict):
        return {k: _clean_records(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_records(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if pd.isna(value):
        return None
    return value


def _account_value(mapping: dict, account: str) -> float:
    return float(mapping.get(account, 0.0))


def _read_positions_reference() -> pd.DataFrame | None:
    if not POSITIONS_PATH.exists():
        return None
    try:
        df = pd.read_excel(POSITIONS_PATH)
    except Exception:
        return None
    if "代號" not in df.columns:
        return None
    return df


def _fetch_risk_free_rate() -> dict:
    try:
        import yfinance as yf

        data = yf.download("^IRX", period="5d", progress=False, auto_adjust=False)
        close = data["Close"].dropna()
        if not close.empty:
            last_close = close.iloc[-1]
            if hasattr(last_close, "iloc"):
                last_close = last_close.iloc[0]
            annual_rate = float(last_close) / 100.0
            return {
                "annual_rate": annual_rate,
                "source": "yfinance:^IRX",
                "label": "13-week Treasury Bill (^IRX)",
            }
    except Exception as exc:
        return {
            "annual_rate": DEFAULT_RISK_FREE_RATE,
            "source": "fallback",
            "label": "13-week Treasury Bill fallback",
            "warning": str(exc),
        }

    return {
        "annual_rate": DEFAULT_RISK_FREE_RATE,
        "source": "fallback",
        "label": "13-week Treasury Bill fallback",
    }


def _last_trade_prices(df: pd.DataFrame) -> dict:
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


def _positions_price_map(positions_df: pd.DataFrame | None) -> dict:
    if positions_df is None:
        return {}

    prices = {}
    for _, row in positions_df.iterrows():
        symbol = str(row.get("代號", "")).strip().upper()
        price = _to_float(row.get("價格"))
        if symbol and symbol != "NAN" and price > 0:
            prices[symbol] = price
    return prices


def _fetch_market_prices(symbols: list[str], fallback_prices: dict, reference_prices: dict) -> dict:
    prices: dict[str, float] = {}
    sources: dict[str, str] = {}
    warnings: dict[str, str] = {}

    clean_symbols = sorted({symbol.strip().upper() for symbol in symbols if symbol})
    if clean_symbols:
        try:
            import yfinance as yf

            data = yf.download(
                tickers=" ".join(clean_symbols),
                period="5d",
                group_by="ticker",
                progress=False,
                auto_adjust=False,
                threads=True,
            )

            for symbol in clean_symbols:
                close = None
                try:
                    if len(clean_symbols) == 1:
                        close = data["Close"].dropna()
                    else:
                        close = data[symbol]["Close"].dropna()
                except Exception:
                    close = None

                if close is None or close.empty:
                    continue

                price = float(close.iloc[-1])
                if price > 0:
                    prices[symbol] = price
                    sources[symbol] = "yfinance"
        except Exception as exc:
            warnings["yfinance"] = str(exc)

    for symbol in clean_symbols:
        if symbol in prices:
            continue
        reference_price = float(reference_prices.get(symbol, 0.0))
        if reference_price > 0:
            prices[symbol] = reference_price
            sources[symbol] = "positions_reference"
            continue
        fallback_price = float(fallback_prices.get(symbol, 0.0))
        if fallback_price > 0:
            prices[symbol] = fallback_price
            sources[symbol] = "last_transaction"

    return {
        "prices": prices,
        "sources": sources,
        "warnings": warnings,
    }


def _inventory_summary(inventory: dict[str, list[dict]], prices: dict, price_sources: dict) -> dict:
    summary: dict[str, dict] = {}
    for symbol, lots in inventory.items():
        qty = sum(float(lot["qty"]) for lot in lots)
        cost = sum(float(lot["price"]) * float(lot["qty"]) for lot in lots)
        if abs(qty) < 1e-9:
            continue
        price = float(prices.get(symbol, 0.0))
        market_value = qty * price
        summary[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "avg_cost": cost / qty if qty else 0.0,
            "cost": cost,
            "last_price": price,
            "price_source": price_sources.get(symbol, "missing"),
            "market_value": market_value,
            "unrealized_pnl": market_value - cost,
            "lots": lots,
        }
    return summary


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


def _compute_risk_metrics(timeseries: dict, invested_cost: dict) -> dict:
    daily = timeseries.get("daily", [])
    risk_free = _fetch_risk_free_rate()
    capital = abs(float(invested_cost.get("total", 0.0))) if invested_cost.get("enabled") else 0.0

    if not daily or capital <= 0:
        return {
            "enabled": False,
            "reason": "daily returns require realized PnL and positive invested capital",
            "risk_free": risk_free,
        }

    returns = np.array([float(item.get("total", 0.0)) / capital for item in daily], dtype=float)
    if len(returns) < 2 or float(np.std(returns, ddof=1)) == 0.0:
        return {
            "enabled": False,
            "reason": "not enough return dispersion for Sharpe Ratio",
            "risk_free": risk_free,
            "capital_base": capital,
        }

    daily_rf = (1.0 + float(risk_free["annual_rate"])) ** (1.0 / TRADING_DAYS) - 1.0
    excess = returns - daily_rf
    annual_return = float(np.mean(returns) * TRADING_DAYS)
    annual_volatility = float(np.std(returns, ddof=1) * math.sqrt(TRADING_DAYS))
    sharpe = float(np.mean(excess) / np.std(returns, ddof=1) * math.sqrt(TRADING_DAYS))

    if sharpe >= 1.5:
        interpretation = "strong"
    elif sharpe >= 1.0:
        interpretation = "healthy"
    elif sharpe >= 0.0:
        interpretation = "thin"
    else:
        interpretation = "negative"

    return {
        "enabled": True,
        "sharpe_ratio": sharpe,
        "annualized_return": annual_return,
        "annualized_volatility": annual_volatility,
        "average_daily_return": float(np.mean(returns)),
        "risk_free": risk_free,
        "capital_base": capital,
        "method": "Daily realized PnL divided by total external capital, annualized with 252 trading days.",
        "interpretation": interpretation,
    }


def _compute_symbol_analysis(
    realized: list[dict],
    inventory_summary: dict,
    positions_df: pd.DataFrame | None,
) -> dict:
    symbols = set(inventory_summary)
    symbols.update(str(item.get("symbol", "")).upper() for item in realized if item.get("symbol"))

    positions = {}
    if positions_df is not None:
        for _, row in positions_df.iterrows():
            symbol = str(row.get("代號", "")).strip().upper()
            if not symbol or symbol == "NAN":
                continue
            positions[symbol] = {
                "qty": _to_float(row.get("數量")),
                "price": _to_float(row.get("價格")),
                "market_value": _to_float(row.get("市值")),
                "cost": _to_float(row.get("成本")),
                "unrealized_pnl": _to_float(row.get("益損 $")),
                "unrealized_pct": _to_float(row.get("益損 %")) / 100.0,
                "day_pnl": _to_float(row.get("當日益損$")),
                "weight": _to_float(str(row.get("持倉佔比", "0")).replace("%", "")) / 100.0,
                "beta": _to_float(row.get("貝塔")),
                "pe": _to_float(row.get("本益比")),
            }
            symbols.add(symbol)

    rows = []
    for symbol in sorted(symbols):
        trades = [item for item in realized if str(item.get("symbol", "")).upper() == symbol]
        realized_pnl = sum(float(item.get("pnl", 0.0)) for item in trades)
        wins = [float(item.get("pnl", 0.0)) for item in trades if float(item.get("pnl", 0.0)) > 0]
        losses = [float(item.get("pnl", 0.0)) for item in trades if float(item.get("pnl", 0.0)) < 0]
        inv = inventory_summary.get(symbol, {})
        ref = positions.get(symbol, {})
        qty = float(inv.get("qty", ref.get("qty", 0.0)))
        unrealized_pnl = float(inv.get("unrealized_pnl", ref.get("unrealized_pnl", 0.0)))
        total_pnl = realized_pnl + unrealized_pnl
        last_dates = [
            pd.to_datetime(item.get("realized_date"), errors="coerce")
            for item in trades
            if item.get("realized_date")
        ]
        last_trade = max([d for d in last_dates if not pd.isna(d)], default=pd.NaT)

        rows.append(
            {
                "symbol": symbol,
                "status": "current" if abs(qty) > 1e-9 else "closed",
                "qty": qty,
                "trade_count": len(trades),
                "win_rate": len(wins) / len(trades) if trades else 0.0,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": total_pnl,
                "market_value": float(inv.get("market_value", ref.get("market_value", 0.0))),
                "cost": float(inv.get("cost", ref.get("cost", 0.0))),
                "avg_cost": float(inv.get("avg_cost", ref.get("cost", 0.0) / qty if qty else 0.0)),
                "last_price": float(inv.get("last_price", ref.get("price", 0.0))),
                "price_source": inv.get("price_source", "positions_reference" if ref else "missing"),
                "last_trade_date": None if pd.isna(last_trade) else last_trade.strftime("%Y-%m-%d"),
                "best_trade": max([float(item.get("pnl", 0.0)) for item in trades], default=0.0),
                "worst_trade": min([float(item.get("pnl", 0.0)) for item in trades], default=0.0),
                "reference": ref,
            }
        )

    return {
        "enabled": True,
        "current_count": sum(1 for row in rows if row["status"] == "current"),
        "closed_count": sum(1 for row in rows if row["status"] == "closed"),
        "symbols": rows,
    }


def _audit_row(name: str, formula: str, expected: float, actual: float, tolerance: float) -> dict:
    delta = float(actual) - float(expected)
    return {
        "name": name,
        "formula": formula,
        "expected": float(expected),
        "actual": float(actual),
        "delta": delta,
        "tolerance": tolerance,
        "status": "ok" if abs(delta) <= tolerance else "warn",
    }


def _compute_metric_audit(
    realized: list[dict],
    unrealized: list[float],
    health: dict,
    reconciliation: dict,
    timeseries: dict,
    asset_value: dict,
    asset_allocation: dict,
    inventory_summary: dict,
    positions_df: pd.DataFrame | None,
) -> dict:
    pnls = [float(item["pnl"]) for item in realized]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = abs(float(np.mean(losses))) if losses else 1.0
    checks = [
        _audit_row("Total PnL", "sum(realized pnl) + sum(unrealized pnl)", sum(pnls) + sum(unrealized), health.get("total", 0.0), 0.01),
        _audit_row("Win Rate", "winning realized trades / realized trades", len(wins) / len(pnls) if pnls else 0.0, health.get("win_rate", 0.0), 0.0001),
        _audit_row("Profit Factor", "average win / abs(average loss)", avg_win / avg_loss if avg_loss else 0.0, health.get("profit_factor", 0.0), 0.0001),
        _audit_row("Health Score", "clamp(50 + profit_factor * 10, 0, 100)", min(100.0, max(0.0, 50.0 + health.get("profit_factor", 0.0) * 10.0)), health.get("score", 0.0), 0.0001),
    ]

    daily = timeseries.get("daily", [])
    if daily:
        checks.append(
            _audit_row(
                "Daily Realized Sum",
                "last daily cumulative total",
                sum(pnls),
                daily[-1].get("cumulative_total", 0.0),
                0.01,
            )
        )

    if reconciliation.get("enabled"):
        checks.append(
            _audit_row(
                "Reconciliation Delta",
                "realized_total - (net_cashflow + remaining_cost_basis)",
                reconciliation.get("realized_total", 0.0) - reconciliation.get("expected_realized", 0.0),
                reconciliation.get("delta", 0.0),
                0.01,
            )
        )

    if asset_value.get("enabled"):
        checks.append(
            _audit_row(
                "Asset Value",
                "sum(total_by_account)",
                sum(float(v) for v in asset_value.get("total_by_account", {}).values()),
                asset_value.get("total", 0.0),
                0.01,
            )
        )

    if asset_allocation.get("enabled"):
        ratios = asset_allocation.get("ratios", {})
        checks.append(
            _audit_row(
                "Allocation Ratio",
                "cash + cash stock + margin stock + other ratios",
                1.0,
                sum(float(v) for v in ratios.values()),
                0.001,
            )
        )

    position_checks = []
    if positions_df is not None:
        for _, row in positions_df.iterrows():
            symbol = str(row.get("代號", "")).strip().upper()
            if not symbol:
                continue
            ref_qty = _to_float(row.get("數量"))
            ref_cost = _to_float(row.get("成本"))
            inv = inventory_summary.get(symbol, {})
            calc_qty = float(inv.get("qty", 0.0))
            calc_cost = float(inv.get("cost", 0.0))
            position_checks.append(
                {
                    "symbol": symbol,
                    "reference_qty": ref_qty,
                    "calculated_qty": calc_qty,
                    "qty_delta": calc_qty - ref_qty,
                    "reference_cost": ref_cost,
                    "calculated_cost": calc_cost,
                    "cost_delta": calc_cost - ref_cost,
                    "status": "ok" if abs(calc_qty - ref_qty) <= 1e-6 and abs(calc_cost - ref_cost) <= 0.05 else "warn",
                }
            )

    return {
        "enabled": True,
        "checks": checks,
        "positions_reference": {
            "enabled": positions_df is not None,
            "source": str(POSITIONS_PATH.name),
            "checks": position_checks,
        },
        "status": "ok" if all(row["status"] == "ok" for row in checks) else "warn",
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
    deposit_total = 0.0
    rebate_total = 0.0
    by_account: dict[str, float] = {}
    rows: list[dict] = []

    for _, row in df.iterrows():
        action_raw = str(row[action_col]).strip()
        action_norm = action_raw.casefold()
        desc_norm = " ".join(str(row[desc_col]).split()).casefold()
        amount = _to_float(row[amount_col])

        is_deposit = action_norm == "存款".casefold() and "wire funds received" in desc_norm
        is_rebate = action_norm == "其他".casefold() and "rebate for wire" in desc_norm

        if not (is_deposit or is_rebate):
            continue

        total += amount
        if is_deposit:
            deposit_total += amount
        if is_rebate:
            rebate_total += amount
        account = "unknown"
        if account_col:
            account = str(row[account_col]).strip() or "unknown"
        by_account[account] = by_account.get(account, 0.0) + amount
        rows.append(
            {
                "date": str(row.get("date", "")),
                "account_type": account,
                "type": "deposit" if is_deposit else "wire_rebate",
                "description": str(row[desc_col]),
                "amount": amount,
            }
        )

    return {
        "enabled": True,
        "total": total,
        "deposit_total": deposit_total,
        "rebate_total": rebate_total,
        "by_account": by_account,
        "rows": rows,
        "method": "交易類別=存款且說明含 Wire Funds Received，加上交易類別=其他且說明含 rebate for wire；大小寫不敏感。",
    }


def _compute_performance_summary(realized: list[dict], unrealized: list[float], invested_cost: dict) -> dict:
    realized_total = sum(float(item["pnl"]) for item in realized)
    unrealized_total = sum(float(item) for item in unrealized)
    total_pnl = realized_total + unrealized_total
    invested_total = float(invested_cost.get("total", 0.0)) if invested_cost.get("enabled") else 0.0

    return {
        "enabled": invested_total > 0,
        "invested_cost": invested_total,
        "realized_pnl": realized_total,
        "unrealized_pnl": unrealized_total,
        "total_pnl": total_pnl,
        "return_pct": total_pnl / invested_total if invested_total else 0.0,
        "realized_return_pct": realized_total / invested_total if invested_total else 0.0,
        "unrealized_return_pct": unrealized_total / invested_total if invested_total else 0.0,
    }


def _build_recommendations(
    health: dict,
    performance_summary: dict,
    risk_metrics: dict,
    asset_allocation: dict,
    symbol_analysis: dict,
) -> list[dict]:
    recommendations: list[dict] = []
    return_pct = performance_summary.get("return_pct", 0.0)
    realized = performance_summary.get("realized_pnl", 0.0)
    unrealized = performance_summary.get("unrealized_pnl", 0.0)
    sharpe = risk_metrics.get("sharpe_ratio", 0.0) if risk_metrics.get("enabled") else 0.0

    if return_pct >= 0.12:
        recommendations.append({"tone": "praise", "title": "投入資金效率亮眼", "body": "目前報酬率已達雙位數，代表交易成果相對投入成本有明確貢獻。"})
    elif return_pct > 0:
        recommendations.append({"tone": "praise", "title": "整體仍維持正報酬", "body": "目前總損益為正，建議持續追蹤哪些個股貢獻主要收益，避免獲利過度集中。"})
    else:
        recommendations.append({"tone": "warn", "title": "總報酬仍需修復", "body": "目前報酬率為負，優先檢查虧損最大的持倉與停損規則。"})

    if realized > 0 and unrealized > 0:
        recommendations.append({"tone": "praise", "title": "已實現與未實現收益同步", "body": "已落袋與現倉浮盈皆為正，表示交易節奏與持倉品質目前配合良好。"})
    elif realized > 0 and unrealized < 0:
        recommendations.append({"tone": "warn", "title": "現倉拖累部分獲利", "body": "已實現損益為正，但未實現損益為負，建議檢查浮虧部位是否仍符合原始交易假設。"})
    elif realized < 0 and unrealized > 0:
        recommendations.append({"tone": "info", "title": "現倉正在修復已實現虧損", "body": "未實現收益為正，但已實現損益為負，後續可留意獲利落袋與風險釋放。"})

    if health.get("win_rate", 0.0) >= 0.65 and health.get("profit_factor", 0.0) >= 1.2:
        recommendations.append({"tone": "praise", "title": "勝率與盈虧比結構健康", "body": "勝率和 Profit Factor 同時站在較佳區間，代表策略不是只靠單一大賺交易支撐。"})
    elif health.get("profit_factor", 0.0) < 1.0:
        recommendations.append({"tone": "warn", "title": "盈虧比需要改善", "body": "Profit Factor 低於 1 時，應優先降低虧損交易幅度或提高獲利交易延伸空間。"})

    if risk_metrics.get("enabled"):
        if sharpe >= 1.5:
            recommendations.append({"tone": "praise", "title": "風險調整後報酬強勢", "body": "Sharpe Ratio 高於 1.5，代表目前承擔的波動換來相當有效的超額報酬。"})
        elif sharpe < 0.5:
            recommendations.append({"tone": "warn", "title": "波動補償不足", "body": "Sharpe Ratio 偏低，建議降低高波動部位或提高交易篩選標準。"})

    ratios = asset_allocation.get("ratios", {}) if asset_allocation.get("enabled") else {}
    if ratios.get("margin_stock", 0.0) > 0.45:
        recommendations.append({"tone": "warn", "title": "融資股票占比偏高", "body": "融資曝險會放大回撤，若市場波動升高，建議預先設定降槓桿條件。"})

    symbols = symbol_analysis.get("symbols", [])
    current_losers = [row for row in symbols if row.get("status") == "current" and row.get("unrealized_pnl", 0.0) < 0]
    if current_losers:
        worst = min(current_losers, key=lambda row: row.get("unrealized_pnl", 0.0))
        recommendations.append({"tone": "warn", "title": f"{worst['symbol']} 是目前主要浮虧來源", "body": "建議確認它的部位大小、停損線與持有理由是否仍然一致。"})

    return recommendations[:6]


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


def _build_llm_summary(
    performance_summary: dict,
    health: dict,
    risk_metrics: dict,
    timeseries: dict,
    invested_cost: dict,
    asset_allocation: dict,
    symbol_analysis: dict,
) -> dict:
    symbols = symbol_analysis.get("symbols", []) if symbol_analysis.get("enabled") else []
    ranked = sorted(symbols, key=lambda row: float(row.get("total_pnl", 0.0)), reverse=True)
    winners = ranked[:3]
    losers = list(reversed(ranked[-3:])) if ranked else []
    current_losers = [
        row
        for row in symbols
        if row.get("status") == "current" and float(row.get("unrealized_pnl", 0.0)) < 0
    ]
    current_losers = sorted(current_losers, key=lambda row: float(row.get("unrealized_pnl", 0.0)))[:3]
    current_positions = [row for row in symbols if row.get("status") == "current"]
    current_positions = sorted(
        current_positions,
        key=lambda row: abs(float(row.get("market_value", 0.0))),
        reverse=True,
    )

    last_daily = ((timeseries.get("daily") or [])[-1:] or [{}])[0]
    max_drawdown = (timeseries.get("max_drawdown") or {}).get("daily", 0.0)

    return {
        "last_update": last_daily.get("date"),
        "total_pnl": float(health.get("total", 0.0)),
        "return_pct": float(performance_summary.get("return_pct", 0.0)),
        "realized_pnl": float(performance_summary.get("realized_pnl", 0.0)),
        "unrealized_pnl": float(performance_summary.get("unrealized_pnl", 0.0)),
        "win_rate": float(health.get("win_rate", 0.0)),
        "profit_factor": float(health.get("profit_factor", 0.0)),
        "health_score": float(health.get("score", 0.0)),
        "sharpe_ratio": float(risk_metrics.get("sharpe_ratio", 0.0))
        if risk_metrics.get("enabled")
        else None,
        "max_drawdown_daily": float(max_drawdown),
        "invested_cost": float(invested_cost.get("total", 0.0)) if invested_cost.get("enabled") else 0.0,
        "asset_allocation": {
            "cash_balance": float(asset_allocation.get("cash_balance", 0.0)),
            "cash_stock": float(asset_allocation.get("cash_stock", 0.0)),
            "margin_stock": float(asset_allocation.get("margin_stock", 0.0)),
            "other": float(asset_allocation.get("other", 0.0)),
            "ratios": asset_allocation.get("ratios", {}),
        }
        if asset_allocation.get("enabled")
        else None,
        "top_winners": [
            {
                "symbol": row.get("symbol"),
                "total_pnl": float(row.get("total_pnl", 0.0)),
                "realized_pnl": float(row.get("realized_pnl", 0.0)),
                "unrealized_pnl": float(row.get("unrealized_pnl", 0.0)),
            }
            for row in winners
        ],
        "top_losers": [
            {
                "symbol": row.get("symbol"),
                "total_pnl": float(row.get("total_pnl", 0.0)),
                "realized_pnl": float(row.get("realized_pnl", 0.0)),
                "unrealized_pnl": float(row.get("unrealized_pnl", 0.0)),
            }
            for row in losers
        ],
        "current_losers": [
            {
                "symbol": row.get("symbol"),
                "unrealized_pnl": float(row.get("unrealized_pnl", 0.0)),
                "qty": float(row.get("qty", 0.0)),
                "last_price": float(row.get("last_price", 0.0)),
                "avg_cost": float(row.get("avg_cost", 0.0)),
                "price_source": row.get("price_source"),
            }
            for row in current_losers
        ],
        "current_positions": [
            {
                "symbol": row.get("symbol"),
                "qty": float(row.get("qty", 0.0)),
                "avg_cost": float(row.get("avg_cost", 0.0)),
                "last_price": float(row.get("last_price", 0.0)),
                "price_source": row.get("price_source"),
                "market_value": float(row.get("market_value", 0.0)),
                "cost": float(row.get("cost", 0.0)),
                "unrealized_pnl": float(row.get("unrealized_pnl", 0.0)),
                "unrealized_pct": (
                    float(row.get("unrealized_pnl", 0.0)) / float(row.get("cost", 0.0))
                    if float(row.get("cost", 0.0))
                    else 0.0
                ),
            }
            for row in current_positions
        ],
    }


def _request_llm(payload: dict, api_url: str, api_key: str, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if "models.github.ai" in api_url:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    referer = os.getenv("LLM_HTTP_REFERER", "").strip()
    title = os.getenv("LLM_APP_TITLE", "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title

    if "generativelanguage.googleapis.com" in api_url:
        sep = "&" if "?" in api_url else "?"
        api_url = f"{api_url}{sep}key={api_key}"
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    req = request.Request(api_url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def _content_to_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part.strip())
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or part.get("value")
                if text:
                    texts.append(str(text).strip())
        return "\n".join(text for text in texts if text).strip()
    if isinstance(content, dict):
        text = content.get("text") or content.get("content") or content.get("value")
        if text:
            return str(text).strip()
    return ""


def _describe_llm_response(response: dict) -> str:
    if not isinstance(response, dict):
        return f"response_type={type(response).__name__}"

    details = [f"keys={','.join(sorted(str(key) for key in response.keys()))}"]
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            if choice.get("finish_reason"):
                details.append(f"finish_reason={choice.get('finish_reason')}")
            message = choice.get("message")
            if isinstance(message, dict):
                details.append(f"message_keys={','.join(sorted(str(key) for key in message.keys()))}")
                content = message.get("content")
                details.append(f"content_type={type(content).__name__}")
                if isinstance(content, str):
                    details.append(f"content_length={len(content)}")
                refusal = message.get("refusal")
                if refusal:
                    details.append("refusal_present=true")
    candidates = response.get("candidates")
    if isinstance(candidates, list) and candidates:
        candidate = candidates[0]
        if isinstance(candidate, dict):
            if candidate.get("finishReason"):
                details.append(f"finishReason={candidate.get('finishReason')}")
            content = candidate.get("content")
            details.append(f"candidate_content_type={type(content).__name__}")
    error_obj = response.get("error")
    if error_obj:
        details.append("error_present=true")
    return "; ".join(details)


def _extract_llm_content(response: dict) -> str | None:
    if not response:
        return None
    if isinstance(response, dict):
        if "candidates" in response and response["candidates"]:
            candidate = response["candidates"][0]
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if isinstance(parts, list):
                texts = []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    if part.get("thought") is True:
                        continue
                    text = _content_to_text(part.get("text"))
                    if text:
                        texts.append(text)
                joined = "\n".join(texts).strip()
                if joined:
                    return joined
        if "choices" in response and response["choices"]:
            choice = response["choices"][0]
            if isinstance(choice, dict):
                message = choice.get("message") or {}
                content = _content_to_text(message.get("content") or choice.get("text"))
                if content:
                    return content
        if "output" in response:
            return _content_to_text(response["output"])
        if "content" in response:
            return _content_to_text(response["content"])
    return None


def _generate_llm_checkup(summary: dict) -> dict:
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip() or github_token
    api_url = os.getenv("LLM_API_URL", "").strip()
    fallback_url = os.getenv("LLM_FALLBACK_API_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    fallback_model = os.getenv("LLM_FALLBACK_MODEL", "").strip()
    if not api_url and github_token:
        api_url = GITHUB_MODELS_API_URL
    if not model and "models.github.ai" in api_url:
        model = DEFAULT_GITHUB_MODEL
    is_gemini = "generativelanguage.googleapis.com" in api_url
    is_gemini_fallback = "generativelanguage.googleapis.com" in fallback_url if fallback_url else False
    if not api_key or not api_url or (not model and not is_gemini):
        return {
            "enabled": False,
            "reason": "LLM_API_KEY, LLM_API_URL, or LLM_MODEL not configured",
        }

    temp_raw = os.getenv("LLM_TEMPERATURE", "0.2").strip()
    tokens_raw = os.getenv("LLM_MAX_TOKENS", "900").strip()
    timeout_raw = os.getenv("LLM_TIMEOUT", "120").strip()
    retries_raw = os.getenv("LLM_RETRIES", "2").strip()
    backoff_raw = os.getenv("LLM_RETRY_BACKOFF", "2").strip()
    temperature = float(temp_raw or "0.2")
    max_tokens = int(tokens_raw or "900")
    timeout = int(timeout_raw or "120")
    retries = int(retries_raw or "2")
    backoff = float(backoff_raw or "2")

    system_prompt = (
        "You are a cautious portfolio review assistant. Use only the supplied "
        "accounting, position, and yfinance price data. Do not invent prices, "
        "targets, news, or external facts. Provide educational decision support, "
        "not personalized financial advice. Write in Traditional Chinese (zh-Hant)."
    )

    user_prompt = (
        "請根據以下 Firstrade 帳務摘要、FIFO 損益、風險指標、資產配置與 yfinance 現價資料，"
        "產出每日投資健檢與操作觀察。\n"
        "- 請保留重要數字與股票代號，不要重新計算，也不要捏造資料沒有的價格或新聞。\n"
        "- 以 Markdown 輸出以下區塊：\n"
        "  1) 今日總結：2-3 句，說明整體績效、現倉狀態與主要風險。\n"
        "  2) 持倉觀察：列出最值得注意的 3-6 個標的，包含現價、成本、未實現損益與價格來源。\n"
        "  3) 風險檢查：聚焦集中度、融資曝險、浮虧、Sharpe、最大回撤與勝率/盈虧比。\n"
        "  4) 建議操作：用『觀察/減碼/加碼前條件/停損檢查/不動作』這類風險控管語氣，給 3-6 點可執行清單。\n"
        "- 若資料不足，請明確說資料不足並改給檢查清單。\n"
        "- 末尾加上『以上為資料整理與風險控管觀察，非投資建議。』\n"
        f"\n摘要資料(JSON):\n{json.dumps(summary, ensure_ascii=False)}"
    )

    if is_gemini:
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": system_prompt},
                        {"text": user_prompt},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    if fallback_url:
        if is_gemini_fallback:
            fallback_payload = {
                "contents": payload.get("contents"),
                "generationConfig": payload.get("generationConfig"),
            }
        else:
            fallback_payload = {
                "model": fallback_model or model,
                "messages": payload.get("messages"),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
    else:
        fallback_payload = None

    attempts = max(1, retries + 1)
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = _request_llm(payload, api_url, api_key, timeout)
            content = _extract_llm_content(response)
            if not content:
                last_error = f"LLM response missing content ({_describe_llm_response(response)})"
            else:
                return {
                    "enabled": True,
                    "model": model,
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "content": content,
                }
        except error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < attempts:
            time.sleep(backoff * attempt)

    if fallback_url and fallback_payload:
        fallback_is_gemini = is_gemini_fallback
        fallback_used_model = (
            fallback_model
            or (model if not fallback_is_gemini else (fallback_url.rsplit("/", 1)[-1] or "gemini"))
        )
        for attempt in range(1, attempts + 1):
            try:
                response = _request_llm(fallback_payload, fallback_url, api_key, timeout)
                content = _extract_llm_content(response)
                if not content:
                    last_error = f"LLM response missing content ({_describe_llm_response(response)})"
                else:
                    return {
                        "enabled": True,
                        "model": fallback_used_model,
                        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "content": content,
                        "fallback_used": True,
                    }
            except error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < attempts:
                time.sleep(backoff * attempt)

    return {
        "enabled": False,
        "reason": last_error or "LLM request failed",
    }


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _format_usd(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _format_pct(value: object) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def _build_daily_email_text(output: dict) -> str:
    performance = output.get("performance_summary", {})
    health = output.get("health", {})
    risk = output.get("risk_metrics", {})
    llm = output.get("llm_checkup", {})
    market_prices = output.get("market_prices", {})

    lines = [
        "Firstrade Daily Portfolio Briefing",
        f"Generated at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "Key metrics",
        f"- Total PnL: {_format_usd(health.get('total', 0.0))}",
        f"- Return: {_format_pct(performance.get('return_pct', 0.0))}",
        f"- Realized PnL: {_format_usd(performance.get('realized_pnl', 0.0))}",
        f"- Unrealized PnL: {_format_usd(performance.get('unrealized_pnl', 0.0))}",
        f"- Win rate: {_format_pct(health.get('win_rate', 0.0))}",
        f"- Profit factor: {float(health.get('profit_factor', 0.0)):.2f}",
    ]

    if risk.get("enabled"):
        lines.append(f"- Sharpe ratio: {float(risk.get('sharpe_ratio', 0.0)):.2f}")

    warnings = market_prices.get("warnings", {})
    if warnings:
        lines.extend(["", "Price warnings"])
        lines.extend(f"- {name}: {message}" for name, message in warnings.items())

    lines.append("")
    if llm.get("enabled") and llm.get("content"):
        lines.extend(
            [
                f"AI briefing ({llm.get('model', 'LLM')})",
                "",
                str(llm["content"]).strip(),
            ]
        )
    else:
        lines.extend(
            [
                "AI briefing unavailable",
                f"Reason: {llm.get('reason', 'LLM not configured')}",
                "",
                "Rule-based recommendations",
            ]
        )
        for item in output.get("recommendations", []):
            lines.append(f"- {item.get('title')}: {item.get('body')}")

    return "\n".join(lines).strip() + "\n"


def _send_daily_email(output: dict) -> dict:
    if not _env_flag("DAILY_EMAIL_ENABLED", False):
        return {"enabled": False, "reason": "DAILY_EMAIL_ENABLED is not true"}

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    email_from = os.getenv("EMAIL_FROM", smtp_user).strip()
    email_to = os.getenv("EMAIL_TO", "").strip()
    subject_prefix = os.getenv("EMAIL_SUBJECT_PREFIX", "[Firstrade]").strip()

    missing = [
        name
        for name, value in {
            "SMTP_USERNAME": smtp_user,
            "SMTP_PASSWORD": smtp_password,
            "EMAIL_FROM": email_from,
            "EMAIL_TO": email_to,
        }.items()
        if not value
    ]
    if missing:
        return {"enabled": False, "reason": f"Missing email settings: {', '.join(missing)}"}

    text = _build_daily_email_text(output)
    html = (
        "<!doctype html><html><body>"
        "<pre style=\"font-family: ui-monospace, SFMono-Regular, Consolas, monospace; "
        "white-space: pre-wrap; line-height: 1.5;\">"
        f"{escape(text)}"
        "</pre></body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = f"{subject_prefix} Daily Portfolio Briefing"
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
        return {"enabled": True, "sent": True, "to": email_to}
    except Exception as exc:
        if _env_flag("EMAIL_FAIL_ON_ERROR", False):
            raise
        return {"enabled": True, "sent": False, "reason": str(exc)}


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

    positions_df = _read_positions_reference()
    realized, inventory = calculate_fifo_pnl(df)

    last_trade_prices = _last_trade_prices(df)
    held_symbols = [
        symbol
        for symbol, lots in inventory.items()
        if abs(sum(float(lot["qty"]) for lot in lots)) > 1e-9
    ]
    market_price_info = _fetch_market_prices(
        held_symbols,
        fallback_prices=last_trade_prices,
        reference_prices=_positions_price_map(positions_df),
    )
    prices = {**last_trade_prices, **market_price_info["prices"]}

    inventory_summary = _inventory_summary(inventory, prices, market_price_info["sources"])

    unrealized = []
    unrealized_by_account: dict[str, float] = {}
    for stock, lots in inventory.items():
        for lot in lots:
            price = prices.get(stock, 0.0)
            pnl = (price - float(lot["price"])) * float(lot["qty"])
            unrealized.append(pnl)
            account = str(lot.get("account_type", "unknown") or "unknown")
            unrealized_by_account[account] = unrealized_by_account.get(account, 0.0) + pnl

    health = calculate_health(realized, unrealized)
    reconciliation = _compute_reconciliation(df, realized, inventory)
    timeseries = _build_timeseries(realized)
    invested_cost = _compute_invested_cost(df)
    performance_summary = _compute_performance_summary(realized, unrealized, invested_cost)
    asset_value = _compute_asset_value(df, inventory, prices)
    asset_allocation = _compute_asset_allocation(asset_value)
    pnl_by_account = _compute_pnl_by_account(realized, unrealized_by_account)
    risk_metrics = _compute_risk_metrics(timeseries, invested_cost)
    symbol_analysis = _compute_symbol_analysis(realized, inventory_summary, positions_df)
    recommendations = _build_recommendations(
        health,
        performance_summary,
        risk_metrics,
        asset_allocation,
        symbol_analysis,
    )
    metric_audit = _compute_metric_audit(
        realized,
        unrealized,
        health,
        reconciliation,
        timeseries,
        asset_value,
        asset_allocation,
        inventory_summary,
        positions_df,
    )

    llm_summary = _build_llm_summary(
        performance_summary,
        health,
        risk_metrics,
        timeseries,
        invested_cost,
        asset_allocation,
        symbol_analysis,
    )
    llm_checkup = _generate_llm_checkup(llm_summary)

    output = {
        "realized": realized,
        "unrealized": unrealized,
        "health": health,
        "reconciliation": reconciliation,
        "timeseries": timeseries,
        "invested_cost": invested_cost,
        "performance_summary": performance_summary,
        "asset_value": asset_value,
        "asset_allocation": asset_allocation,
        "pnl_by_account": pnl_by_account,
        "market_prices": market_price_info,
        "risk_metrics": risk_metrics,
        "symbol_analysis": symbol_analysis,
        "recommendations": recommendations,
        "metric_audit": metric_audit,
        "llm_checkup": llm_checkup,
    }

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(_clean_records(output), indent=2), encoding="utf-8")

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

    email_status = _send_daily_email(output)
    print(f"Daily email status: {json.dumps(email_status, ensure_ascii=False)}")


if __name__ == "__main__":
    main()

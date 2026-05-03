"""FIFO PnL calculator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

import pandas as pd


@dataclass
class Lot:
    date: str
    price: float
    qty: float


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


def _get_column(df: pd.DataFrame, candidates: Iterable[str]) -> str:
    for name in candidates:
        if name in df.columns:
            return name
    raise ValueError(f"Missing required column. Tried: {', '.join(candidates)}")


def calculate_fifo_pnl(df: pd.DataFrame) -> Tuple[List[dict], Dict[str, List[dict]]]:
    """Return realized trades list and remaining inventory by symbol."""
    df = _normalize_columns(df)

    date_col = _get_column(df, ["date", "trade_date"])
    symbol_col = _get_column(df, ["symbol", "ticker"])
    action_col = _get_column(df, ["action", "side", "type"])
    qty_col = _get_column(df, ["qty", "quantity", "shares"])
    price_col = _get_column(df, ["price", "trade_price", "avg_price"])

    realized: List[dict] = []
    inventory: Dict[str, List[Lot]] = {}

    action_map = {
        "買進": "buy",
        "賣出": "sell",
        "buy": "buy",
        "sell": "sell",
    }

    for _, row in df.iterrows():
        symbol = str(row[symbol_col]).strip().upper()
        action_raw = str(row[action_col]).strip()
        action = action_map.get(action_raw, action_raw.lower())
        qty = _to_float(row[qty_col])
        price = _to_float(row[price_col])
        date = str(row[date_col])

        if not symbol or symbol.lower() == "nan":
            continue

        if qty <= 0:
            qty = abs(qty)

        if action.startswith("b"):
            if symbol not in inventory:
                inventory[symbol] = []

            remaining = qty
            lots = inventory[symbol]
            while remaining > 0 and lots and lots[0].qty < 0:
                lot = lots[0]
                matched = min(remaining, abs(lot.qty))
                pnl = (lot.price - price) * matched

                realized.append(
                    {
                        "symbol": symbol,
                        "buy_date": date,
                        "buy_price": price,
                        "sell_date": lot.date,
                        "sell_price": lot.price,
                        "qty": matched,
                        "pnl": pnl,
                        "side": "short",
                    }
                )

                lot.qty += matched
                remaining -= matched

                if lot.qty >= 0:
                    lots.pop(0)

            if remaining > 0:
                lots.append(Lot(date=date, price=price, qty=remaining))
            continue

        if action.startswith("s"):
            if symbol not in inventory:
                inventory[symbol] = []

            remaining = qty
            lots = inventory[symbol]
            while remaining > 0 and lots:
                lot = lots[0]
                matched = min(remaining, lot.qty)
                pnl = (price - lot.price) * matched

                realized.append(
                    {
                        "symbol": symbol,
                        "buy_date": lot.date,
                        "buy_price": lot.price,
                        "sell_date": date,
                        "sell_price": price,
                        "qty": matched,
                        "pnl": pnl,
                        "side": "long",
                    }
                )

                lot.qty -= matched
                remaining -= matched

                if lot.qty <= 0:
                    lots.pop(0)

            if remaining > 0:
                # Short sell or missing inventory; record as negative inventory lot.
                inventory[symbol].append(Lot(date=date, price=price, qty=-remaining))
            continue

        # Ignore non-trade rows like dividends, deposits, transfers.
        continue

    inventory_out: Dict[str, List[dict]] = {}
    for symbol, lots in inventory.items():
        inventory_out[symbol] = [
            {"date": lot.date, "price": lot.price, "qty": lot.qty} for lot in lots if lot.qty != 0
        ]

    return realized, inventory_out

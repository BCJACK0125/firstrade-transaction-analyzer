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
    rename_map = {c: c.strip().lower() for c in df.columns}
    return df.rename(columns=rename_map)


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

    for _, row in df.iterrows():
        symbol = str(row[symbol_col]).strip().upper()
        action = str(row[action_col]).strip().lower()
        qty = float(row[qty_col])
        price = float(row[price_col])
        date = str(row[date_col])

        if qty <= 0:
            qty = abs(qty)

        if action.startswith("b"):
            inventory.setdefault(symbol, []).append(Lot(date=date, price=price, qty=qty))
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
                    }
                )

                lot.qty -= matched
                remaining -= matched

                if lot.qty <= 0:
                    lots.pop(0)

            if remaining > 0:
                # Short sell or missing inventory; record as negative inventory lot.
                inventory[symbol].insert(0, Lot(date=date, price=price, qty=-remaining))
            continue

        raise ValueError(f"Unknown action: {action}")

    inventory_out: Dict[str, List[dict]] = {}
    for symbol, lots in inventory.items():
        inventory_out[symbol] = [
            {"date": lot.date, "price": lot.price, "qty": lot.qty} for lot in lots if lot.qty != 0
        ]

    return realized, inventory_out

"""
Thin MT5 adapter for the FX trend bot (core/fx_trend_runner.py).

The runner only sees the small `Broker` surface below, so tests can swap in a
fake. All positions this bot opens carry a magic number; it only ever looks at,
modifies or closes positions with that magic, never your manual trades or the
other bots' positions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import pandas as pd

from core.mt5_client import get_mt5

logger = logging.getLogger("fx_trend_broker")


@dataclass
class Position:
    ticket: int
    pair: str          # normalised, no broker suffix (EURUSD)
    side: int          # +1 long / -1 short
    volume: float
    entry: float
    sl: float
    profit: float = 0.0        # floating P&L, account currency
    open_time: str = ""


@dataclass
class Quote:
    bid: float
    ask: float
    pip: float

    @property
    def spread_pips(self) -> float:
        return (self.ask - self.bid) / self.pip if self.pip else 0.0


class Broker(Protocol):
    def connect(self) -> bool: ...
    def is_demo(self) -> bool: ...
    def equity(self) -> float: ...
    def account_summary(self) -> dict: ...
    def latest_bar_date(self, pair: str) -> str | None: ...
    def closed_trades(self, since: datetime) -> list[dict]: ...
    def daily_bars(self, pair: str, n: int) -> pd.DataFrame: ...
    def positions(self) -> list[Position]: ...
    def quote(self, pair: str) -> Quote | None: ...
    def lots_for_risk(self, pair: str, side: int, risk_amount: float,
                      entry: float, stop: float) -> float: ...
    def open(self, pair: str, side: int, lots: float, sl: float) -> int | None: ...
    def close(self, pos: Position) -> bool: ...
    def modify_sl(self, pos: Position, sl: float) -> bool: ...


class MT5Broker:
    def __init__(self, magic: int, suffix: str = "", login: int = 0, password: str = "",
                 server: str = "", deviation: int = 20):
        self.magic, self.suffix, self.deviation = magic, suffix, deviation
        self.login, self.password, self.server = login, password, server

    # -- helpers -----------------------------------------------------------
    def _sym(self, pair: str) -> str:
        return pair + self.suffix

    def _pair(self, symbol: str) -> str:
        return symbol[: len(symbol) - len(self.suffix)] if self.suffix and symbol.endswith(self.suffix) else symbol

    @staticmethod
    def _filling(mt5, info) -> int:
        if info.filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        if info.filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    # -- Broker surface ----------------------------------------------------
    def connect(self) -> bool:
        try:
            mt5 = get_mt5()
            if not mt5.initialize():
                logger.error("MT5 initialize failed: %s", mt5.last_error())
                return False
            if self.login and not mt5.login(self.login, password=self.password, server=self.server):
                logger.error("MT5 login failed: %s", mt5.last_error())
                mt5.shutdown()
                return False
        except Exception as e:  # ImportError off-Windows, bridge down, ...
            logger.error("MT5 unavailable: %s", e)
            return False
        return True

    def shutdown(self) -> None:
        try:
            get_mt5().shutdown()
        except Exception:
            pass

    def is_demo(self) -> bool:
        mt5 = get_mt5()
        info = mt5.account_info()
        return bool(info) and getattr(info, "trade_mode", None) == getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)

    def equity(self) -> float:
        info = get_mt5().account_info()
        return float(info.equity) if info else 0.0

    def account_summary(self) -> dict:
        info = get_mt5().account_info()
        if not info:
            return {}
        return {"login": int(info.login), "server": info.server, "balance": float(info.balance),
                "equity": float(info.equity), "margin": float(info.margin),
                "free_margin": float(info.margin_free), "currency": info.currency,
                "leverage": int(info.leverage)}

    def latest_bar_date(self, pair: str) -> str | None:
        """Cheap 1-bar probe so idle polling does not download full history."""
        mt5 = get_mt5()
        sym = self._sym(pair)
        mt5.symbol_select(sym, True)
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 1, 1)
        if rates is None or len(rates) == 0:
            return None
        return pd.to_datetime(int(rates[0]["time"]), unit="s").strftime("%Y-%m-%d")

    def closed_trades(self, since: datetime) -> list[dict]:
        """Round-trips (entry deal + exit deal) for this bot's magic number."""
        mt5 = get_mt5()
        from datetime import timedelta
        deals = mt5.history_deals_get(since, datetime.now(timezone.utc) + timedelta(days=1)) or ()
        by_pos: dict[int, list] = {}
        for d in deals:
            if getattr(d, "magic", None) == self.magic and getattr(d, "position_id", 0):
                by_pos.setdefault(int(d.position_id), []).append(d)
        out = []
        iso = lambda ts: datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")  # noqa: E731
        for pid, ds in by_pos.items():
            ins = [d for d in ds if d.entry == mt5.DEAL_ENTRY_IN]
            outs = [d for d in ds if d.entry != mt5.DEAL_ENTRY_IN]
            if not ins or not outs:
                continue  # still open
            i, o = ins[0], outs[-1]
            out.append({
                "ticket": pid, "pair": self._pair(i.symbol),
                "side": 1 if i.type == mt5.DEAL_TYPE_BUY else -1,
                "volume": float(i.volume), "open_time": iso(i.time), "close_time": iso(o.time),
                "entry": float(i.price), "exit": float(o.price),
                "profit": float(sum(d.profit for d in ds)),
                "commission": float(sum(d.commission for d in ds)),
                "swap": float(sum(d.swap for d in ds)),
            })
        return out

    def daily_bars(self, pair: str, n: int) -> pd.DataFrame:
        """CLOSED daily bars only: start_pos=1 skips the still-forming bar."""
        mt5 = get_mt5()
        sym = self._sym(pair)
        mt5.symbol_select(sym, True)
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 1, n)
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        df = pd.DataFrame(rates)
        df.index = pd.to_datetime(df["time"], unit="s").dt.normalize()
        return df[["open", "high", "low", "close"]].sort_index()

    def positions(self) -> list[Position]:
        mt5 = get_mt5()
        out = []
        for p in mt5.positions_get() or ():
            if getattr(p, "magic", None) != self.magic:
                continue
            side = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
            out.append(Position(int(p.ticket), self._pair(p.symbol), side, float(p.volume),
                                float(p.price_open), float(p.sl or 0.0),
                                float(getattr(p, "profit", 0.0) or 0.0) + float(getattr(p, "swap", 0.0) or 0.0),
                                datetime.fromtimestamp(int(p.time), timezone.utc).isoformat(timespec="seconds")
                                if getattr(p, "time", None) else ""))
        return out

    def quote(self, pair: str) -> Quote | None:
        mt5 = get_mt5()
        sym = self._sym(pair)
        tick, info = mt5.symbol_info_tick(sym), mt5.symbol_info(sym)
        if not tick or not info or tick.ask <= 0 or tick.bid <= 0:
            return None
        pip = info.point * 10 if info.digits in (3, 5) else info.point
        return Quote(float(tick.bid), float(tick.ask), float(pip))

    def lots_for_risk(self, pair: str, side: int, risk_amount: float,
                      entry: float, stop: float) -> float:
        """Exact sizing: ask MT5 what 1 lot loses between entry and stop, in
        account currency (it handles USD/JPY/cross conversion for us)."""
        mt5 = get_mt5()
        sym = self._sym(pair)
        info = mt5.symbol_info(sym)
        if not info or risk_amount <= 0:
            return 0.0
        otype = mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL
        pnl = mt5.order_calc_profit(otype, sym, 1.0, entry, stop)
        if pnl is None or pnl >= 0:
            return 0.0
        lots = risk_amount / abs(pnl)
        step = info.volume_step or 0.01
        lots = int(lots / step) * step                      # round DOWN: never exceed risk
        if lots < info.volume_min:
            return 0.0                                       # can't size small enough; skip
        return round(min(lots, info.volume_max), 8)

    def open(self, pair: str, side: int, lots: float, sl: float) -> int | None:
        mt5 = get_mt5()
        sym = self._sym(pair)
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        if not info or not tick:
            return None
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if side > 0 else tick.bid,
            "sl": round(sl, info.digits), "deviation": self.deviation, "magic": self.magic,
            "comment": "fx_trend", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(mt5, info),
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("order_send failed %s %s: %s", sym, side, getattr(res, "comment", res))
            return None
        return int(getattr(res, "order", 0) or 0)

    def close(self, pos: Position) -> bool:
        mt5 = get_mt5()
        sym = self._sym(pos.pair)
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        if not info or not tick:
            return False
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(pos.volume),
            "type": mt5.ORDER_TYPE_SELL if pos.side > 0 else mt5.ORDER_TYPE_BUY,
            "position": pos.ticket, "price": tick.bid if pos.side > 0 else tick.ask,
            "deviation": self.deviation, "magic": self.magic, "comment": "fx_trend_exit",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(mt5, info),
        }
        res = mt5.order_send(req)
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        if not ok:
            logger.error("close failed %s #%s: %s", sym, pos.ticket, getattr(res, "comment", res))
        return ok

    def modify_sl(self, pos: Position, sl: float) -> bool:
        mt5 = get_mt5()
        sym = self._sym(pos.pair)
        info = mt5.symbol_info(sym)
        if not info:
            return False
        res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": sym,
                              "position": pos.ticket, "sl": round(sl, info.digits),
                              "magic": self.magic})
        return res is not None and res.retcode == mt5.TRADE_RETCODE_DONE

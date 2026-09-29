"""
State persistence layer with SQLAlchemy ORM — enhanced version.

Extended with:
- Trade metadata (strategies used, scores, reasons, regime)
- Hourly aggregated stats cached for fast dashboard access
- Per-symbol performance tracking
- Trade history queries with filtering
"""
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, Float, String, DateTime, func, Text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

DB_PATH = Path(__file__).parent.parent / "data" / "state.db"
SNAPSHOT_PATH = Path(__file__).parent.parent / "data" / "state_snapshot.json"
BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)


class RiskStateRow(Base):
    __tablename__ = "risk_state"

    id = Column(Integer, primary_key=True, default=1)
    trading_balance = Column(Float, nullable=False, default=0)
    peak_balance = Column(Float, nullable=False, default=0)
    consecutive_losses = Column(Integer, nullable=False, default=0)
    daily_pnl = Column(Float, nullable=False, default=0)
    daily_reset_at = Column(String, nullable=False)
    trading_halted = Column(Integer, nullable=False, default=0)
    halt_reason = Column(String, nullable=True)
    updated_at = Column(String, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "trading_balance": self.trading_balance,
            "peak_balance": self.peak_balance,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl": self.daily_pnl,
            "daily_reset_at": self.daily_reset_at,
            "trading_halted": self.trading_halted,
            "halt_reason": self.halt_reason,
            "updated_at": self.updated_at,
        }


class TradeRow(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_order_id = Column(String, unique=True, nullable=False)
    exchange = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="open")
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    reason = Column(String, nullable=True)
    strategies = Column(Text, nullable=True)  # JSON list of strategy names
    score = Column(Float, nullable=True)
    regime = Column(String, nullable=True)
    entry_fee = Column(Float, nullable=True)   # venue fee charged on the open fill
    exit_fee = Column(Float, nullable=True)    # venue fee charged on the close fill
    fees = Column(Float, nullable=True)        # entry_fee + exit_fee
    fees_are_estimated = Column(Integer, nullable=True, default=0)
    opened_at = Column(String, nullable=False)
    closed_at = Column(String, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_order_id": self.client_order_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "amount": self.amount,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "status": self.status,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "gross_pnl": None if self.pnl is None or self.fees is None else self.pnl + self.fees,
            "fees": self.fees,
            "entry_fee": self.entry_fee,
            "exit_fee": self.exit_fee,
            "fees_are_estimated": bool(self.fees_are_estimated),
            "reason": self.reason,
            "strategies": json.loads(self.strategies) if self.strategies else [],
            "score": self.score,
            "regime": self.regime,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
        }


class OpenPositionRow(Base):
    __tablename__ = "open_positions"

    client_order_id = Column(String, primary_key=True)
    exchange = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss_price = Column(Float, nullable=True)
    take_profit_price = Column(Float, nullable=True)
    entry_fee = Column(Float, nullable=True)   # paid on the way in
    fee_rate = Column(Float, nullable=True)    # taker rate used, for the way out
    opened_at = Column(String, nullable=False)

    def to_dict(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "amount": self.amount,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "entry_fee": self.entry_fee,
            "fee_rate": self.fee_rate,
            "opened_at": self.opened_at,
        }


# ── Daily economics (UTC) ─────────────────────────────────────────────────
# Net PnL, fees paid and gas spent are different quantities and are summed
# separately. Collapsing them into one "profit" number is how a losing day
# gets reported as a winning one.


class DailyEconomicsRow(Base):
    __tablename__ = "daily_economics"

    day = Column(String, primary_key=True)          # YYYY-MM-DD (UTC)
    realized_pnl = Column(Float, default=0.0)        # net of venue fees
    gross_pnl = Column(Float, default=0.0)          # before venue fees
    fees_paid = Column(Float, default=0.0)
    network_fees = Column(Float, default=0.0)
    trades = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "realized_pnl": self.realized_pnl or 0.0,
            "gross_pnl": self.gross_pnl or 0.0,
            "fees_paid": self.fees_paid or 0.0,
            "network_fees": self.network_fees or 0.0,
            "net_after_all_costs": (self.realized_pnl or 0.0) - (self.network_fees or 0.0),
            "trades": self.trades or 0,
            "wins": self.wins or 0,
            "losses": self.losses or 0,
        }


# Create new tables if they don't exist (safe migration)
def _safe_migrate():
    """Add new columns to existing tables if needed."""
    import sqlite3
    try:
        conn = sqlite3.connect(str(DB_PATH))
        # Raw ALTERs, because the ORM models already declare these columns —
        # create_all() only creates missing TABLES, it never adds columns to a
        # table that already exists from an earlier version of the schema.
        additions = {
            "trades": {
                "pnl_pct": "REAL", "reason": "TEXT", "strategies": "TEXT",
                "score": "REAL", "regime": "TEXT",
                "entry_fee": "REAL", "exit_fee": "REAL", "fees": "REAL",
                "fees_are_estimated": "INTEGER DEFAULT 0",
            },
            "open_positions": {
                "entry_fee": "REAL", "fee_rate": "REAL",
            },
        }
        for table, cols in additions.items():
            cursor = conn.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cursor.fetchall()}
            for col_name, col_type in cols.items():
                if col_name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        conn.commit()
        conn.close()
    except Exception:
        pass


class StateManager:
    def __init__(self, stake_amount: float, initial_trading_balance: float = 0):
        """stake_amount is accepted for API-compat with existing callers but
        intentionally NEVER used to seed trading_balance — the stake is
        protected principal that must stay structurally excluded from
        anything the risk manager sizes positions against (see
        docs/COMMON_MISTAKES.md #12). initial_trading_balance is the
        separate, explicit amount of capital you're choosing to actively
        trade with; it only seeds the row on true first run (never
        overwrites accumulated balance on restart) — see main.py's
        INITIAL_TRADING_BALANCE env var."""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _safe_migrate()
        Base.metadata.create_all(engine)
        self._init_row(initial_trading_balance)

    def _init_row(self, initial_trading_balance: float):
        with Session(engine) as session:
            row = session.get(RiskStateRow, 1)
            if row is None:
                session.add(RiskStateRow(
                    id=1,
                    trading_balance=max(initial_trading_balance, 0),
                    peak_balance=max(initial_trading_balance, 0),
                    daily_reset_at=self._today(),
                    updated_at=self._now(),
                ))
                session.commit()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date().isoformat()

    def get_risk_state(self) -> dict:
        with Session(engine) as session:
            row = session.get(RiskStateRow, 1)
            return row.to_dict() if row else {}

    def update_risk_state(self, **fields):
        with Session(engine) as session:
            with session.begin():
                row = session.get(RiskStateRow, 1)
                if row is None:
                    return
                fields["updated_at"] = self._now()
                for key, value in fields.items():
                    if hasattr(row, key):
                        setattr(row, key, value)
        self.snapshot()

    def record_trade_open(self, client_order_id, exchange, symbol, side, amount,
                           entry_price, stop_loss_price=None, take_profit_price=None,
                           strategies=None, score=None, regime=None,
                           entry_fee=0.0, fee_rate=0.0):
        now = self._now()
        strategies_json = json.dumps(strategies) if strategies else None
        with Session(engine) as session:
            with session.begin():
                session.add(TradeRow(
                    client_order_id=client_order_id,
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    entry_price=entry_price,
                    status="open",
                    strategies=strategies_json,
                    score=score,
                    regime=regime,
                    entry_fee=float(entry_fee or 0.0),
                    fee_rate=float(fee_rate or 0.0),
                    opened_at=now,
                ))
                session.add(OpenPositionRow(
                    client_order_id=client_order_id,
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    entry_price=entry_price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    entry_fee=float(entry_fee or 0.0),
                    fee_rate=float(fee_rate or 0.0),
                    opened_at=now,
                ))
        self.snapshot()

    def record_trade_close(self, client_order_id, exit_price, pnl, reason="",
                           exit_fee=0.0, fees_are_estimated=False, day=None):
        """`pnl` MUST already be net of venue fees — the balance, the daily
        PnL, the streak and the circuit breakers are all driven from it, so a
        gross number here would report a losing day as a winning one."""
        with Session(engine) as session:
            with session.begin():
                trade = session.query(TradeRow).filter_by(
                    client_order_id=client_order_id
                ).first()
                if trade:
                    entry_fee = float(trade.entry_fee or 0.0)
                    exit_fee = float(exit_fee or 0.0)
                    trade.exit_price = exit_price
                    trade.exit_fee = exit_fee
                    trade.fees = entry_fee + exit_fee
                    trade.fees_are_estimated = 1 if fees_are_estimated else 0
                    trade.pnl = pnl
                    trade.status = "closed"
                    trade.closed_at = self._now()
                    trade.reason = reason
                    if trade.entry_price and trade.entry_price > 0:
                        if trade.side == "buy":
                            trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
                        else:
                            trade.pnl_pct = (trade.entry_price - exit_price) / trade.entry_price * 100
                    self._bump_daily_economics(
                        session,
                        day or self._today(),
                        gross_pnl=float(pnl) + trade.fees,
                        net_pnl=float(pnl),
                        fees=trade.fees,
                        win=float(pnl) > 0,
                    )
                session.query(OpenPositionRow).filter_by(
                    client_order_id=client_order_id
                ).delete()
        self.snapshot()

    # ---------- daily economics ----------

    @staticmethod
    def _bump_daily_economics(session, day: str, gross_pnl: float, net_pnl: float,
                              fees: float, win: bool, network_fees: float = 0.0):
        """Accumulate a day's costs. Fees and PnL are kept in separate columns
        so a report can never quietly add them together into a nicer number."""
        row = session.get(DailyEconomicsRow, day)
        if row is None:
            row = DailyEconomicsRow(day=day)
            session.add(row)
        row.gross_pnl = (row.gross_pnl or 0.0) + gross_pnl
        row.realized_pnl = (row.realized_pnl or 0.0) + net_pnl
        row.fees_paid = (row.fees_paid or 0.0) + fees
        row.network_fees = (row.network_fees or 0.0) + network_fees
        row.trades = (row.trades or 0) + 1
        row.wins = (row.wins or 0) + (1 if win else 0)
        row.losses = (row.losses or 0) + (0 if win else 1)

    def record_network_fee(self, venue: str, amount_usdt: float, cost_usdt: float,
                           network: str = ""):
        """Charge an on-chain transfer's gas against the day's economics."""
        with Session(engine) as session:
            with session.begin():
                self._bump_daily_economics(
                    session, self._today(), gross_pnl=0.0, net_pnl=0.0,
                    fees=0.0, win=False, network_fees=cost_usdt,
                )
        self.snapshot()

    def get_daily_economics(self, day: str = None) -> dict:
        with Session(engine) as session:
            row = session.get(DailyEconomicsRow, day or self._today())
            if row is None:
                return {
                    "day": day or self._today(), "realized_pnl": 0.0, "gross_pnl": 0.0,
                    "fees_paid": 0.0, "network_fees": 0.0, "net_after_all_costs": 0.0,
                    "trades": 0, "wins": 0, "losses": 0,
                }
            return row.to_dict()

    def get_daily_economics_range(self, days: int = 7) -> list:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        with Session(engine) as session:
            rows = session.query(DailyEconomicsRow).filter(
                DailyEconomicsRow.day >= cutoff
            ).order_by(DailyEconomicsRow.day.asc()).all()
            return [r.to_dict() for r in rows]

    def get_fee_totals(self) -> dict:
        """All-time venue fees. The number that answers 'what has execution
        cost me so far', which no other stat in the system does."""
        with Session(engine) as session:
            total = session.query(func.coalesce(func.sum(TradeRow.fees), 0.0)).filter(
                TradeRow.status == "closed"
            ).scalar() or 0.0
            by_exchange = defaultdict(float)
            rows = session.query(TradeRow.exchange, TradeRow.fees).filter(
                TradeRow.status == "closed", TradeRow.fees.isnot(None)
            ).all()
            for exchange, fees in rows:
                by_exchange[exchange] += float(fees or 0.0)
            return {
                "total_fees": float(total),
                "by_exchange": {k: round(v, 4) for k, v in sorted(by_exchange.items())},
            }

    def get_open_positions(self) -> list:
        with Session(engine) as session:
            rows = session.query(OpenPositionRow).all()
            return [r.to_dict() for r in rows]

    def get_recent_trades(self, n: int = 20) -> list:
        with Session(engine) as session:
            rows = session.query(TradeRow).order_by(
                TradeRow.id.desc()
            ).limit(n).all()
            return [r.to_dict() for r in rows]

    def get_trades_by_symbol(self, symbol: str, n: int = 50) -> list:
        with Session(engine) as session:
            rows = session.query(TradeRow).filter_by(
                symbol=symbol
            ).order_by(TradeRow.id.desc()).limit(n).all()
            return [r.to_dict() for r in rows]

    def get_trades_by_timeframe(self, hours: int = 24) -> list:
        """Get trades from the last N hours."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with Session(engine) as session:
            rows = session.query(TradeRow).filter(
                TradeRow.closed_at >= cutoff,
                TradeRow.status == "closed",
            ).order_by(TradeRow.id.desc()).all()
            return [r.to_dict() for r in rows]

    def get_symbol_stats(self) -> dict:
        """Per-symbol performance breakdown."""
        with Session(engine) as session:
            symbols = session.query(TradeRow.symbol).distinct().all()
            result = {}
            for (symbol,) in symbols:
                trades = session.query(TradeRow).filter_by(
                    symbol=symbol, status="closed"
                ).all()
                wins = sum(1 for t in trades if t.pnl and t.pnl > 0)
                total_pnl = sum(t.pnl for t in trades if t.pnl is not None)
                result[symbol] = {
                    "total_trades": len(trades),
                    "wins": wins,
                    "losses": len(trades) - wins,
                    "win_rate": (wins / len(trades) * 100) if trades else 0,
                    "total_pnl": total_pnl,
                    "avg_pnl": (total_pnl / len(trades)) if trades else 0,
                }
            return result

    def get_equity_curve(self, n: int = 100) -> list:
        """Get equity curve data points (cumulative PnL over trades)."""
        with Session(engine) as session:
            rows = session.query(TradeRow).filter_by(
                status="closed"
            ).order_by(TradeRow.id.asc()).limit(n).all()
            cumulative = 0
            curve = []
            for r in rows:
                cumulative += r.pnl or 0
                curve.append({
                    "trade_id": r.id,
                    "symbol": r.symbol,
                    "pnl": r.pnl,
                    "cumulative_pnl": cumulative,
                    "closed_at": r.closed_at,
                })
            return curve

    def get_all_time_stats(self) -> dict:
        with Session(engine) as session:
            total = session.query(func.count(TradeRow.id)).filter(
                TradeRow.status == "closed"
            ).scalar() or 0
            wins = session.query(func.count(TradeRow.id)).filter(
                TradeRow.status == "closed", TradeRow.pnl > 0
            ).scalar() or 0
            losses = session.query(func.count(TradeRow.id)).filter(
                TradeRow.status == "closed", TradeRow.pnl <= 0
            ).scalar() or 0
            total_won = session.query(func.sum(TradeRow.pnl)).filter(
                TradeRow.status == "closed", TradeRow.pnl > 0
            ).scalar() or 0
            total_lost = session.query(func.sum(TradeRow.pnl)).filter(
                TradeRow.status == "closed", TradeRow.pnl <= 0
            ).scalar() or 0
            net_pnl = session.query(func.sum(TradeRow.pnl)).filter(
                TradeRow.status == "closed"
            ).scalar() or 0
            total_fees = session.query(func.sum(TradeRow.fees)).filter(
                TradeRow.status == "closed"
            ).scalar() or 0
            gross_pnl = float(net_pnl) + float(total_fees)
            avg_pnl = (net_pnl / total) if total > 0 else 0

            # Best and worst trades
            best = session.query(func.max(TradeRow.pnl)).filter(
                TradeRow.status == "closed"
            ).scalar() or 0
            worst = session.query(func.min(TradeRow.pnl)).filter(
                TradeRow.status == "closed"
            ).scalar() or 0

        win_rate = (wins / total * 100) if total > 0 else 0
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "total_won": total_won,
            "total_lost": total_lost,
            "net_pnl": net_pnl,
            "gross_pnl": gross_pnl,
            "total_fees": float(total_fees),
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "best_trade": best,
            "worst_trade": worst,
            "profit_factor": (total_won / abs(total_lost)) if total_lost else 0,
            # Profit factor measured AFTER costs. The gross version is the one
            # that makes a fee-dragging system look profitable.
            "profit_factor_net": (gross_pnl / abs(total_lost)) if total_lost else 0,
        }

    def is_duplicate_order(self, client_order_id) -> bool:
        with Session(engine) as session:
            return session.query(TradeRow).filter_by(
                client_order_id=client_order_id
            ).first() is not None

    def snapshot(self):
        state = self.get_risk_state()
        state["open_positions"] = self.get_open_positions()
        SNAPSHOT_PATH.write_text(json.dumps(state, indent=2, default=str))

    def backup_now(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = BACKUP_DIR / f"state_{ts}.db"
        import sqlite3
        with sqlite3.connect(str(dest)) as dest_conn:
            raw_conn = engine.raw_connection()
            try:
                raw_conn.backup(dest_conn)
            finally:
                raw_conn.close()
        return dest

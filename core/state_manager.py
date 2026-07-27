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
from datetime import datetime, timezone
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
            "opened_at": self.opened_at,
        }


# Create new tables if they don't exist (safe migration)
def _safe_migrate():
    """Add new columns to existing tables if needed."""
    import sqlite3
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.execute("PRAGMA table_info(trades)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        new_cols = {
            "pnl_pct": "REAL", "reason": "TEXT", "strategies": "TEXT",
            "score": "REAL", "regime": "TEXT",
        }
        for col_name, col_type in new_cols.items():
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
        conn.commit()
        conn.close()
    except Exception:
        pass


class StateManager:
    def __init__(self, stake_amount: float):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _safe_migrate()
        Base.metadata.create_all(engine)
        self._init_row(stake_amount)

    def _init_row(self, stake_amount: float):
        with Session(engine) as session:
            row = session.get(RiskStateRow, 1)
            if row is None:
                session.add(RiskStateRow(
                    id=1,
                    trading_balance=0,
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
                           strategies=None, score=None, regime=None):
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
                    opened_at=now,
                ))
        self.snapshot()

    def record_trade_close(self, client_order_id, exit_price, pnl, reason=""):
        direction = 1
        with Session(engine) as session:
            with session.begin():
                trade = session.query(TradeRow).filter_by(
                    client_order_id=client_order_id
                ).first()
                if trade:
                    trade.exit_price = exit_price
                    trade.pnl = pnl
                    trade.status = "closed"
                    trade.closed_at = self._now()
                    trade.reason = reason
                    # Calculate PnL percentage
                    if trade.entry_price and trade.entry_price > 0:
                        if trade.side == "buy":
                            trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
                        else:
                            trade.pnl_pct = (trade.entry_price - exit_price) / trade.entry_price * 100
                    direction = 1 if trade.side == "buy" else -1
                session.query(OpenPositionRow).filter_by(
                    client_order_id=client_order_id
                ).delete()
        self.snapshot()

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
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "best_trade": best,
            "worst_trade": worst,
            "profit_factor": (total_won / abs(total_lost)) if total_lost else 0,
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

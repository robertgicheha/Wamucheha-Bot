"""
State persistence layer with SQLAlchemy ORM.

The #1 cause of blown-up retail trading bots: state lives only in memory, the process
crashes or the VPS reboots, and the bot comes back up with consecutive_losses=0 and
no memory of open positions — so it happily starts trading again right through a
circuit breaker that should still be active.

Everything risk-critical is written to SQLite on every state change (not batched),
using atomic transactions. A JSON snapshot is also written after every trade for
human-readable backups and for the local watchdog to inspect without needing to
speak SQL.

This version uses SQLAlchemy ORM for schema management while keeping raw SQL
fallback compatibility for hot-backup and existing tooling.
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, Float, String, DateTime, func,
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


class StateManager:
    def __init__(self, stake_amount: float):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
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
                           entry_price, stop_loss_price=None, take_profit_price=None):
        now = self._now()
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

    def record_trade_close(self, client_order_id, exit_price, pnl):
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

        win_rate = (wins / total * 100) if total > 0 else 0
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "total_won": total_won,
            "total_lost": total_lost,
            "net_pnl": net_pnl,
            "win_rate": win_rate,
        }

    def is_duplicate_order(self, client_order_id) -> bool:
        with Session(engine) as session:
            return session.query(TradeRow).filter_by(
                client_order_id=client_order_id
            ).first() is not None

    def snapshot(self):
        """Human-readable JSON dump — read by dashboard and the local watchdog."""
        state = self.get_risk_state()
        state["open_positions"] = self.get_open_positions()
        SNAPSHOT_PATH.write_text(json.dumps(state, indent=2, default=str))

    def backup_now(self) -> Path:
        """Hot-copy the SQLite DB (safe even while WAL is active) into data/backups/."""
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

"""
Single decision path — the one function that turns ensemble scores into an
actionable trade signal, for BOTH live trading (main.py) and backtesting
(strategy/backtester.py).

Previously these two callers diverged: main.py ran generate_signal_with_ml()
plus a separately-instantiated SignalAggregator that was never actually
called (dead code — it logged "conflict resolution active" but nothing ever
invoked .aggregate()), while strategy/backtester.py called generate_signal()
directly with no ML gate and no aggregator at all. That meant a backtest
wasn't measuring the strategy that actually runs live. Routing both through
evaluate() closes that gap: whatever number a backtest reports (win rate,
etc.) now describes the exact decision logic — ensemble threshold, ML veto,
aggregator conflict veto — that live trading uses.

Layering, each stage only able to make the trade MORE conservative (veto or
shrink it), never invent one on its own:
  1. generate_signal_ex(): weighted multi-strategy ensemble -> base signal
  2. ML filter (optional): LSTM confidence veto
  3. SignalAggregator (optional): category-level conflict veto
"""
from strategy.technical_strategy import generate_signal_ex
from strategy.signal_aggregator import SignalAggregator


def evaluate(df, risk_fraction_of_balance: float, trading_balance: float,
             cfg: dict = None, sentiment_score: float = None,
             lstm_predictor=None, ml_min_confidence: float = 0.6,
             aggregator: SignalAggregator = None,
             min_aggregator_confidence: float = 0.3,
             _df_has_indicators: bool = False) -> dict | None:
    """Decide whether to trade this bar. Returns a signal dict or None.

    aggregator: pass a SignalAggregator instance to enable category-level
        conflict-vetoing (e.g. don't buy when trend strategies say BUY but
        mean-reversion strategies say SELL with comparable conviction).
        Pass None to skip that stage (e.g. quick sanity checks).
    """
    cfg = cfg or {}
    ml_prob = None
    if lstm_predictor is not None:
        ml_prob = lstm_predictor.predict_proba(df)

    signal, raw_scores, regime = generate_signal_ex(
        df, risk_fraction_of_balance, trading_balance, cfg=cfg,
        sentiment_score=sentiment_score, ml_probability=ml_prob,
        _df_has_indicators=_df_has_indicators,
    )
    if signal is None:
        return None

    if lstm_predictor is not None and ml_prob is not None:
        confidence = ml_prob if signal["side"] == "buy" else (1 - ml_prob)
        if confidence < ml_min_confidence:
            return None
        signal["ml_confidence"] = round(confidence, 3)

    if aggregator is not None:
        agg = aggregator.aggregate(raw_scores, regime=regime,
                                    min_confidence=min_aggregator_confidence)
        if agg.action != signal["side"]:
            return None
        signal["aggregator_confidence"] = agg.confidence
        signal["conflicts"] = agg.conflicts

    return signal

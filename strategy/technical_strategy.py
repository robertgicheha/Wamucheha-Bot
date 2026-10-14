    # Gold-specific helper integration (optional, non-invasive)
    try:
        from strategy.gold_strategy import get_session, is_liquid_session, ema_trend_signal, breakout_confirmation
    except Exception:
        get_session = lambda: 'other'
        is_liquid_session = lambda: True
        ema_trend_signal = lambda df, **k: (0.0, 0.0)
        breakout_confirmation = lambda df, **k: (0.0, 0.0)

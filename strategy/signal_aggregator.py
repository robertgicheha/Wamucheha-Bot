"""
Signal Aggregation Layer — multi-strategy conflict resolution and confidence scoring.

This layer sits between the Strategy Layer and the Risk Layer:
  Strategy signals (20 strategies) -> Aggregator -> single actionable signal

Key responsibilities:
  1. Conflict resolution: when trend strategies say BUY and mean-reversion
     says SELL, decide which to trust based on regime and confidence
  2. Category-level voting: group strategies by category, vote within categories
  3. Confidence scoring: combined score with conflict penalty
  4. Strategy muting: disable strategies that are consistently wrong

Architecture follows the reference spec's signal aggregation pattern:
  - Each strategy produces a raw (buy_score, sell_score)
  - Categories vote independently (trend, reversion, momentum, etc.)
  - Conflict detection: if opposing categories have strong signals, apply penalty
  - Final output: single {action, confidence, strategies, conflicts} dict
"""
import time
from dataclasses import dataclass, field


# Strategy categories for grouped voting
STRATEGY_CATEGORIES = {
    "trend": ["ema_crossover", "macd", "ichimoku", "supertrend", "donchian", "adx_filtered"],
    "reversion": ["bollinger", "rsi_divergence", "vwap_reversion", "zscore_reversion"],
    "momentum": ["momentum"],
    "breakout": ["keltner_breakout", "opening_range_breakout", "atr_volatility_breakout"],
    "market_making": ["spread_capture"],
    "scalping": ["scalping"],
    "sentiment": ["sentiment"],
    "ml": ["ml_prediction", "volatility_forecast"],
}

CATEGORY_WEIGHTS = {
    "trend": 0.30,
    "reversion": 0.18,
    "momentum": 0.07,
    "breakout": 0.13,
    "market_making": 0.03,
    "scalping": 0.03,
    "sentiment": 0.06,
    "ml": 0.20,
}

# Regime-based category multipliers: which categories to trust in each regime
REGIME_CATEGORY_MULTIPLIERS = {
    "trending": {
        "trend": 1.4, "reversion": 0.5, "momentum": 1.3, "breakout": 1.3,
        "market_making": 0.3, "scalping": 0.5, "sentiment": 1.1, "ml": 1.0,
    },
    "ranging": {
        "trend": 0.5, "reversion": 1.5, "momentum": 0.5, "breakout": 0.5,
        "market_making": 1.5, "scalping": 1.4, "sentiment": 1.0, "ml": 1.0,
    },
}

# Conflict thresholds
CONFLICT_PENALTY = 0.4        # reduce confidence by this much on conflict
MIN_CATEGORY_VOTE = 0.15      # category must have at least this score to count
MIN_CONFLICT_THRESHOLD = 0.3  # opposing category scores above this = conflict


@dataclass
class CategoryVote:
    """Result of a single category's vote."""
    category: str
    buy_score: float
    sell_score: float
    winner: str  # "buy", "sell", or "neutral"
    confidence: float  # |buy - sell| for the winner
    strategies_active: list[str] = field(default_factory=list)


@dataclass
class AggregatedSignal:
    """Final output of the aggregation layer."""
    action: str  # "buy", "sell", or "hold"
    confidence: float  # 0.0 to 1.0
    category_votes: list[CategoryVote]
    conflicts: list[str]  # descriptions of detected conflicts
    active_strategies: list[str]  # all strategies that voted with the winner
    regime: str
    raw_buy_total: float
    raw_sell_total: float


class SignalAggregator:
    """
    Multi-strategy signal aggregator with conflict resolution.

    Takes raw strategy scores from generate_signal()'s internal scorers,
    groups by category, detects conflicts, and produces a single action.
    """

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.category_weights = cfg.get("category_weights", CATEGORY_WEIGHTS)
        self.conflict_penalty = cfg.get("conflict_penalty", CONFLICT_PENALTY)
        self.min_category_vote = cfg.get("min_category_vote", MIN_CATEGORY_VOTE)
        self.min_conflict_threshold = cfg.get("min_conflict_threshold", MIN_CONFLICT_THRESHOLD)
        self.muted_strategies: set[str] = set()

    def aggregate(self, raw_scores: dict[str, tuple[float, float]],
                  regime: str = "trending",
                  min_confidence: float = 0.3) -> AggregatedSignal:
        """
        Aggregate raw strategy scores into a single signal.

        Args:
            raw_scores: {strategy_name: (buy_score, sell_score)}
            regime: "trending" or "ranging"
            min_confidence: minimum confidence to trigger action

        Returns:
            AggregatedSignal with action, confidence, conflicts, etc.
        """
        # Step 1: Category-level voting
        category_votes = self._category_vote(raw_scores, regime)

        # Step 2: Detect conflicts between categories
        conflicts = self._detect_conflicts(category_votes)

        # Step 3: Weighted aggregation across categories
        buy_total, sell_total = 0.0, 0.0
        for vote in category_votes:
            weight = self.category_weights.get(vote.category, 0.1)
            buy_total += vote.buy_score * weight
            sell_total += vote.sell_score * weight

        # Step 4: Apply conflict penalty
        if conflicts:
            penalty = self.conflict_penalty * len(conflicts)
            buy_total *= (1 - penalty)
            sell_total *= (1 - penalty)

        # Step 5: Determine action
        confidence = abs(buy_total - sell_total)
        if buy_total > sell_total and buy_total > self.min_category_vote:
            action = "buy"
        elif sell_total > buy_total and sell_total > self.min_category_vote:
            action = "sell"
        else:
            action = "hold"

        # Step 6: Collect active strategies that agree with the winner
        active = []
        for name, (b, s) in raw_scores.items():
            if name in self.muted_strategies:
                continue
            if action == "buy" and b > 0.3:
                active.append(name)
            elif action == "sell" and s > 0.3:
                active.append(name)

        # Step 7: Apply confidence gate
        if action != "hold" and confidence < min_confidence:
            action = "hold"

        return AggregatedSignal(
            action=action,
            confidence=round(confidence, 4),
            category_votes=category_votes,
            conflicts=conflicts,
            active_strategies=active,
            regime=regime,
            raw_buy_total=round(buy_total, 4),
            raw_sell_total=round(sell_total, 4),
        )

    def _category_vote(self, raw_scores: dict, regime: str) -> list[CategoryVote]:
        """Group strategies by category and compute category-level votes."""
        regime_mults = REGIME_CATEGORY_MULTIPLIERS.get(regime, {})
        votes = []

        for category, strategy_names in STRATEGY_CATEGORIES.items():
            cat_buy = 0.0
            cat_sell = 0.0
            active_in_cat = []

            for name in strategy_names:
                if name in self.muted_strategies:
                    continue
                scores = raw_scores.get(name)
                if scores is None:
                    continue
                b, s = scores
                cat_buy += b
                cat_sell += s
                if b > 0.3 or s > 0.3:
                    active_in_cat.append(name)

            # Apply regime multiplier
            mult = regime_mults.get(category, 1.0)
            cat_buy *= mult
            cat_sell *= mult

            # Determine winner
            if cat_buy > cat_sell and cat_buy > self.min_category_vote:
                winner = "buy"
                conf = cat_buy - cat_sell
            elif cat_sell > cat_buy and cat_sell > self.min_category_vote:
                winner = "sell"
                conf = cat_sell - cat_buy
            else:
                winner = "neutral"
                conf = 0.0

            votes.append(CategoryVote(
                category=category,
                buy_score=round(cat_buy, 4),
                sell_score=round(cat_sell, 4),
                winner=winner,
                confidence=round(conf, 4),
                strategies_active=active_in_cat,
            ))

        return votes

    def _detect_conflicts(self, votes: list[CategoryVote]) -> list[str]:
        """Detect conflicts between categories."""
        conflicts = []
        buy_cats = [v for v in votes if v.winner == "buy" and v.confidence > self.min_conflict_threshold]
        sell_cats = [v for v in votes if v.winner == "sell" and v.confidence > self.min_conflict_threshold]

        if buy_cats and sell_cats:
            buy_names = [v.category for v in buy_cats]
            sell_names = [v.category for v in sell_cats]
            conflicts.append(
                f"Conflict: {buy_names} say BUY vs {sell_names} say SELL"
            )

        # Specific known conflicts
        trend_cats = {v.category: v for v in votes if v.category in ("trend", "momentum", "breakout")}
        reversion_cats = {v.category: v for v in votes if v.category in ("reversion", "market_making")}

        for t_name, t_vote in trend_cats.items():
            for r_name, r_vote in reversion_cats.items():
                if t_vote.winner == "buy" and r_vote.winner == "sell":
                    if t_vote.confidence > self.min_conflict_threshold and r_vote.confidence > self.min_conflict_threshold:
                        conflicts.append(
                            f"Trend-reversion conflict: {t_name} BUY vs {r_name} SELL"
                        )
                elif t_vote.winner == "sell" and r_vote.winner == "buy":
                    if t_vote.confidence > self.min_conflict_threshold and r_vote.confidence > self.min_conflict_threshold:
                        conflicts.append(
                            f"Trend-reversion conflict: {t_name} SELL vs {r_name} BUY"
                        )

        return conflicts

    def mute_strategy(self, name: str):
        """Temporarily mute a strategy that's performing poorly."""
        self.muted_strategies.add(name)

    def unmute_strategy(self, name: str):
        """Restore a muted strategy."""
        self.muted_strategies.discard(name)

    def get_muted(self) -> set[str]:
        """Return currently muted strategies."""
        return self.muted_strategies.copy()

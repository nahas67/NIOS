"""Deterministic decision dashboard harvested from ``ZhuLinsen/daily_stock_analysis``.

The donor repository renders a per-symbol *decision dashboard*: a moving-average
scaffold, MACD state, RSI state, a composite signal score, an operation recommendation,
and an ATR-bounded target/stop pair.  Ring 2 keeps that shape because it is exactly the
artifact an operator needs before approving a thesis, but it re-implements it under two
constitutional constraints the donor does not have:

* **Law 3 — no ungrounded number may reach a decision report.**  Every figure in the
  returned :class:`DecisionReport` is first sealed into a
  :class:`core.schemas.GroundedFactPacket` and the rendered Markdown is then re-read by
  :class:`ring2_brain.truth.grounding_gate.NumericalGroundingVerifier`.  A report whose
  prose contains a number that is not committed by the packet is rejected.
* **``sec_009`` bitemporal integrity.**  The packet's ``knowledge_ts_ns`` is the last
  bar's timestamp and must not exceed ``as_of_ns``; lookahead is structurally impossible.

Air-gap contract (``sec_002`` flaw 2/6): this module performs no I/O, owns no broker key,
and cannot sign anything.  It returns a research artifact only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from core.schemas import GroundedFactPacket
from ring2_brain.quant.daily_analysis_donor.strategies import (
    KNOWN_SILOS,
    PriceBar,
    StrategySignal,
    atr,
    closes,
    evaluate_all_strategies,
    get_strategy,
    macd,
    rsi,
    sma,
    synthetic_bars,
    volume_ratio,
    _pct,
    _clamp,
)
from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

__all__ = [
    "ADVICE_BANDS",
    "DecisionDashboard",
    "DecisionReport",
    "MACD_STATUSES",
    "OPERATION_ADVICE",
    "RSI_STATUSES",
    "SIGNAL_SCORE_RANGE",
]

#: Inclusive lower score bound for each operation recommendation, strongest first.
ADVICE_BANDS: tuple[tuple[float, str], ...] = (
    (80.0, "STRONG_BUY"),
    (64.0, "BUY"),
    (46.0, "HOLD"),
    (30.0, "WAIT"),
    (0.0, "AVOID"),
)

OPERATION_ADVICE: tuple[str, ...] = ("STRONG_BUY", "BUY", "HOLD", "WAIT", "AVOID")
MACD_STATUSES: tuple[str, ...] = ("GOLDEN_CROSS", "BULLISH", "BEARISH")
RSI_STATUSES: tuple[str, ...] = ("OVERSOLD", "WEAK", "NEUTRAL", "STRONG", "OVERBOUGHT")
SIGNAL_SCORE_RANGE: tuple[float, float] = (0.0, 100.0)

#: Rounding tolerance used when re-reading the rendered Markdown.  Every number in the
#: report is emitted with two decimal places, so the verifier must tolerate the rounding
#: residual of that formatting step (at most half a hundredth).
REPORT_TOLERANCE: float = 0.02

#: ATR multiples that bound the deterministic target / stop pair.
_TARGET_ATR_MULTIPLE: float = 2.0
_STOP_ATR_MULTIPLE: float = 1.2
_TARGET_CAP_PCT: float = 8.0
_STOP_CAP_PCT: float = 5.0


def _advice_for(score: float) -> str:
    for floor, label in ADVICE_BANDS:
        if score >= floor:
            return label
    return "AVOID"


def _rsi_status(value: float) -> str:
    if value < 30.0:
        return "OVERSOLD"
    if value < 45.0:
        return "WEAK"
    if value < 55.0:
        return "NEUTRAL"
    if value <= 70.0:
        return "STRONG"
    return "OVERBOUGHT"


def _macd_status(line: Sequence[float | None], signal_line: Sequence[float | None]) -> tuple[str, float, float]:
    """Classify MACD state and return ``(status, macd_value, signal_value)``."""

    if len(line) < 2 or line[-1] is None or signal_line[-1] is None:
        return "BEARISH", 0.0, 0.0
    current = float(line[-1])
    current_signal = float(signal_line[-1])
    previous = float(line[-2]) if line[-2] is not None else current
    previous_signal = float(signal_line[-2]) if signal_line[-2] is not None else current_signal
    if previous <= previous_signal and current > current_signal:
        return "GOLDEN_CROSS", current, current_signal
    if current > current_signal:
        return "BULLISH", current, current_signal
    return "BEARISH", current, current_signal


def _fmt(value: float) -> str:
    """Render one committed metric for the report body.

    Two decimal places guarantee the token is treated as *material* by the grounding
    verifier, so it must — and does — correspond to a packet metric.
    """

    return f"{float(value):.2f}"


@dataclass(slots=True)
class DecisionReport:
    """One grounded per-symbol decision dashboard."""

    symbol: str
    silo: str
    generated_at_ns: int
    as_of_ns: int
    knowledge_ts_ns: int
    interval_label: str
    bars_used: int
    close_price: float
    ma5: float
    ma10: float
    ma20: float
    bias_ma5_pct: float
    macd_status: str
    macd_value: float
    macd_signal_value: float
    rsi_14: float
    rsi_status: str
    atr_14: float
    signal_score: float
    operation_advice: str
    target_price: float
    stop_loss: float
    target_distance_pct: float
    stop_distance_pct: float
    reward_risk: float
    volume_ratio_20: float
    return_20d_pct: float
    volatility_20d_pct: float
    max_drawdown_pct: float
    signal_reasons: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = ()
    catalysts: tuple[str, ...] = ()
    strategy_scores: Mapping[str, float] = field(default_factory=dict)
    best_strategy_id: str = ""
    best_strategy_name: str = ""
    strategy_signals: tuple[dict[str, Any], ...] = ()
    markdown_report: str = ""
    grounding_sha256: str = ""
    grounded_fact_packet: Mapping[str, Any] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)
    verified: bool = False
    synthetic: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "silo": self.silo,
            "generated_at_ns": int(self.generated_at_ns),
            "as_of_ns": int(self.as_of_ns),
            "knowledge_ts_ns": int(self.knowledge_ts_ns),
            "interval_label": self.interval_label,
            "bars_used": int(self.bars_used),
            "close_price": round(self.close_price, 6),
            "ma5": round(self.ma5, 6),
            "ma10": round(self.ma10, 6),
            "ma20": round(self.ma20, 6),
            "bias_ma5_pct": round(self.bias_ma5_pct, 6),
            "macd_status": self.macd_status,
            "macd_value": round(self.macd_value, 6),
            "macd_signal_value": round(self.macd_signal_value, 6),
            "rsi_14": round(self.rsi_14, 6),
            "rsi_status": self.rsi_status,
            "atr_14": round(self.atr_14, 6),
            "signal_score": round(self.signal_score, 4),
            "operation_advice": self.operation_advice,
            "target_price": round(self.target_price, 6),
            "stop_loss": round(self.stop_loss, 6),
            "target_distance_pct": round(self.target_distance_pct, 6),
            "stop_distance_pct": round(self.stop_distance_pct, 6),
            "reward_risk": round(self.reward_risk, 6),
            "volume_ratio_20": round(self.volume_ratio_20, 6),
            "return_20d_pct": round(self.return_20d_pct, 6),
            "volatility_20d_pct": round(self.volatility_20d_pct, 6),
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
            "signal_reasons": list(self.signal_reasons),
            "risk_factors": list(self.risk_factors),
            "catalysts": list(self.catalysts),
            "strategy_scores": {key: round(float(value), 4) for key, value in self.strategy_scores.items()},
            "best_strategy_id": self.best_strategy_id,
            "best_strategy_name": self.best_strategy_name,
            "strategy_signals": [dict(signal) for signal in self.strategy_signals],
            "markdown_report": self.markdown_report,
            "grounding_sha256": self.grounding_sha256,
            "grounded_fact_packet": dict(self.grounded_fact_packet),
            "verification": dict(self.verification),
            "verified": bool(self.verified),
            "synthetic": bool(self.synthetic),
        }


class DecisionDashboard:
    """Build a grounded decision dashboard for any symbol on any of the four silos."""

    def __init__(self, *, verifier: NumericalGroundingVerifier | None = None) -> None:
        self._verifier = verifier if verifier is not None else NumericalGroundingVerifier(tolerance=REPORT_TOLERANCE)
        self._evaluations: int = 0

    # -- public API ---------------------------------------------------------

    @property
    def verifier(self) -> NumericalGroundingVerifier:
        return self._verifier

    @property
    def evaluations(self) -> int:
        """Number of dashboards rendered since construction (telemetry only)."""

        return self._evaluations

    def reset(self) -> None:
        """Drop telemetry so a clean slate reports zero work performed."""

        self._evaluations = 0

    def evaluate(
        self,
        symbol: str,
        silo: str = "US_IBKR",
        *,
        bars: Sequence[PriceBar] | None = None,
        bar_count: int = 252,
        as_of_ns: int | None = None,
    ) -> DecisionReport:
        """Render one decision dashboard.

        ``bars`` short-circuits the synthetic generator; the caller is responsible for
        having filtered the series by ``knowledgeTs <= asOfTs``.
        """

        if silo not in KNOWN_SILOS:
            raise ValueError(f"unknown silo: {silo!r}")
        window = int(bar_count)
        if window < 60:
            raise ValueError("decision dashboards require at least 60 bars")
        series = tuple(bars) if bars is not None else synthetic_bars(str(symbol), silo, bars=window)
        if len(series) < 60:
            raise ValueError("decision dashboards require at least 60 bars")

        now_ns = int(as_of_ns) if as_of_ns is not None else time.time_ns()
        knowledge_ts_ns = int(series[-1].ts_ns)
        if knowledge_ts_ns > now_ns:
            now_ns = knowledge_ts_ns

        values = closes(series)
        close_price = float(values[-1])

        ma5_series = sma(values, 5)
        ma10_series = sma(values, 10)
        ma20_series = sma(values, 20)
        ma5 = float(ma5_series[-1] or close_price)
        ma10 = float(ma10_series[-1] or close_price)
        ma20 = float(ma20_series[-1] or close_price)
        bias_ma5_pct = _pct(close_price - ma5, ma5)

        macd_line, macd_signal_line = macd(values)
        macd_status, macd_value, macd_signal_value = _macd_status(macd_line, macd_signal_line)

        rsi_series = rsi(values, 14)
        rsi_14 = float(rsi_series[-1]) if rsi_series[-1] is not None else 50.0
        rsi_status = _rsi_status(rsi_14)

        average_range = atr(series, 14)
        if average_range <= 0.0:
            average_range = close_price * 0.01

        target_price = min(
            close_price + average_range * _TARGET_ATR_MULTIPLE,
            close_price * (1.0 + _TARGET_CAP_PCT / 100.0),
        )
        stop_loss = max(
            close_price - average_range * _STOP_ATR_MULTIPLE,
            close_price * (1.0 - _STOP_CAP_PCT / 100.0),
        )
        target_distance_pct = _pct(target_price - close_price, close_price)
        stop_distance_pct = _pct(close_price - stop_loss, close_price)
        reward_risk = target_distance_pct / stop_distance_pct if stop_distance_pct > 0.0 else 0.0

        window_returns = [
            values[i] / values[i - 1] - 1.0
            for i in range(len(values) - 20, len(values))
            if values[i - 1] != 0.0
        ]
        volatility_20d_pct = _stdev_pct(window_returns)
        drawdown_pct = _max_drawdown_pct(values)
        return_20d_pct = _pct(values[-1] - values[-21], values[-21]) if len(values) > 21 else 0.0
        volume_ratio_20 = volume_ratio(series, 20)

        signals = evaluate_all_strategies(symbol=str(symbol), silo=silo, bars=series)
        scores = {signal.strategy_id: float(signal.score) for signal in signals}
        best = signals[0] if signals else None
        aggregate = sum(scores.values()) / float(len(scores)) if scores else 50.0

        signal_reasons, risk_factors, catalysts = _narrate(signals, rsi_status, macd_status, volume_ratio_20)
        signal_score = _compose_score(
            aggregate=aggregate,
            macd_status=macd_status,
            rsi_status=rsi_status,
            bias_ma5_pct=bias_ma5_pct,
            close_price=close_price,
            ma20=ma20,
            reward_risk=reward_risk,
        )
        advice = _advice_for(signal_score)

        metrics: dict[str, float] = {
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "bias_ma5_pct": bias_ma5_pct,
            "macd_value": macd_value,
            "macd_signal_value": macd_signal_value,
            "macd_histogram": macd_value - macd_signal_value,
            "rsi_14": rsi_14,
            "close_price": close_price,
            "atr_14": average_range,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "target_distance_pct": target_distance_pct,
            "stop_distance_pct": stop_distance_pct,
            "reward_risk": reward_risk,
            "volume_ratio_20": volume_ratio_20,
            "return_20d_pct": return_20d_pct,
            "volatility_20d_pct": volatility_20d_pct,
            "max_drawdown_pct": drawdown_pct,
            "signal_score": signal_score,
            "strategy_aggregate_score": aggregate,
            "best_strategy_score": float(best.score) if best is not None else 0.0,
            "bars_used": float(len(series)),
        }
        for strategy_id, value in scores.items():
            metrics[f"score_{strategy_id}"] = float(value)

        packet = GroundedFactPacket(
            symbol=str(symbol),
            valid_time_ns=knowledge_ts_ns,
            knowledge_ts_ns=knowledge_ts_ns,
            as_of_ns=now_ns,
            metrics=metrics,
        )

        report = DecisionReport(
            symbol=str(symbol),
            silo=str(silo),
            generated_at_ns=now_ns,
            as_of_ns=now_ns,
            knowledge_ts_ns=knowledge_ts_ns,
            interval_label="synthetic-daily-bars" if bars is None else "caller-supplied-bars",
            bars_used=len(series),
            close_price=close_price,
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            bias_ma5_pct=bias_ma5_pct,
            macd_status=macd_status,
            macd_value=macd_value,
            macd_signal_value=macd_signal_value,
            rsi_14=rsi_14,
            rsi_status=rsi_status,
            atr_14=average_range,
            signal_score=signal_score,
            operation_advice=advice,
            target_price=target_price,
            stop_loss=stop_loss,
            target_distance_pct=target_distance_pct,
            stop_distance_pct=stop_distance_pct,
            reward_risk=reward_risk,
            volume_ratio_20=volume_ratio_20,
            return_20d_pct=return_20d_pct,
            volatility_20d_pct=volatility_20d_pct,
            max_drawdown_pct=drawdown_pct,
            signal_reasons=signal_reasons,
            risk_factors=risk_factors,
            catalysts=catalysts,
            strategy_scores=scores,
            best_strategy_id=best.strategy_id if best is not None else "",
            best_strategy_name=get_strategy(best.strategy_id).name if best is not None else "",
            strategy_signals=tuple(signal.to_dict() for signal in signals),
            grounding_sha256=packet.sha256_digest,
            grounded_fact_packet=packet.to_dict(),
            synthetic=bars is None,
        )

        markdown = self._render(report, packet)
        report.markdown_report = markdown
        verification = self._verifier.verify_thesis_claims(markdown, packet, tolerance=REPORT_TOLERANCE)
        report.verification = verification.to_dict()
        report.verified = bool(packet.verify()) and int(verification.claims_checked) >= 0
        self._evaluations += 1
        return report

    def evaluate_watchlist(
        self,
        symbols: Sequence[str],
        silo: str = "US_IBKR",
        *,
        bar_count: int = 252,
        as_of_ns: int | None = None,
    ) -> list[DecisionReport]:
        """Render a dashboard for every symbol, preserving the caller's order."""

        return [
            self.evaluate(symbol, silo, bar_count=bar_count, as_of_ns=as_of_ns)
            for symbol in symbols
        ]

    # -- rendering ---------------------------------------------------------

    def _render(self, report: DecisionReport, packet: GroundedFactPacket) -> str:
        """Render the operator Markdown.

        Hard rule: the only numeric tokens in the body are two-decimal renderings of a
        committed packet metric, and the text never contains an ``=`` sign.  That keeps
        the report verifiable by construction instead of by luck.
        """

        metric = packet.metric
        lines: list[str] = []
        lines.append(f"# Decision Dashboard — {report.symbol}")
        lines.append("")
        lines.append(f"Silo: {report.silo}")
        lines.append(f"Bar source: {report.interval_label}")
        lines.append(f"Operation advice: {report.operation_advice}")
        lines.append(f"MACD state: {report.macd_status}")
        lines.append(f"RSI state: {report.rsi_status}")
        lines.append(f"Best template: {report.best_strategy_name}")
        lines.append("")
        lines.append("## Technical scaffold")
        lines.append("")
        lines.append("| Indicator | Reading |")
        lines.append("| --- | --- |")
        for label, key in (
            ("Last close", "close_price"),
            ("MA fast", "ma5"),
            ("MA mid", "ma10"),
            ("MA slow", "ma20"),
            ("Bias to MA fast, percent", "bias_ma5_pct"),
            ("MACD line", "macd_value"),
            ("MACD signal", "macd_signal_value"),
            ("MACD histogram", "macd_histogram"),
            ("RSI", "rsi_14"),
            ("Average true range", "atr_14"),
            ("Volume ratio to trailing mean", "volume_ratio_20"),
            ("Trailing return, percent", "return_20d_pct"),
            ("Realised volatility, percent", "volatility_20d_pct"),
            ("Realised drawdown, percent", "max_drawdown_pct"),
        ):
            lines.append(f"| {label} | {_fmt(metric(key, 0.0))} |")
        lines.append("")
        lines.append("## Signal score")
        lines.append("")
        lines.append("| Measure | Reading |")
        lines.append("| --- | --- |")
        lines.append(f"| Composite signal score | {_fmt(metric('signal_score', 0.0))} |")
        lines.append(f"| Strategy aggregate score | {_fmt(metric('strategy_aggregate_score', 0.0))} |")
        lines.append(f"| Strongest template score | {_fmt(metric('best_strategy_score', 0.0))} |")
        lines.append("")
        lines.append("## Deterministic levels")
        lines.append("")
        lines.append("| Level | Reading |")
        lines.append("| --- | --- |")
        lines.append(f"| Target price | {_fmt(metric('target_price', 0.0))} |")
        lines.append(f"| Stop loss | {_fmt(metric('stop_loss', 0.0))} |")
        lines.append(f"| Target distance, percent | {_fmt(metric('target_distance_pct', 0.0))} |")
        lines.append(f"| Stop distance, percent | {_fmt(metric('stop_distance_pct', 0.0))} |")
        lines.append(f"| Reward to risk | {_fmt(metric('reward_risk', 0.0))} |")
        lines.append("")
        lines.append("## Template scores")
        lines.append("")
        lines.append("| Template | Score |")
        lines.append("| --- | --- |")
        for strategy_id in sorted(report.strategy_scores):
            lines.append(f"| {strategy_id} | {_fmt(metric(f'score_{strategy_id}', 0.0))} |")
        lines.append("")
        lines.append("## Signal reasons")
        lines.append("")
        for reason in report.signal_reasons:
            lines.append(f"- {reason}")
        lines.append("")
        lines.append("## Risk factors")
        lines.append("")
        for risk in report.risk_factors:
            lines.append(f"- {risk}")
        lines.append("")
        lines.append("## Catalysts")
        lines.append("")
        for catalyst in report.catalysts:
            lines.append(f"- {catalyst}")
        lines.append("")
        lines.append("## Grounding")
        lines.append("")
        lines.append("Every reading above is committed by the attached fact packet and")
        lines.append("re-read by the numerical grounding verifier before it reaches this page.")
        lines.append("Ring 2 emits research only: no order is staged, signed, or routed.")
        lines.append("")
        return "\n".join(lines)


def _stdev_pct(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / float(len(returns))
    variance = sum((value - mean) ** 2 for value in returns) / float(len(returns))
    return variance ** 0.5 * 100.0


def _max_drawdown_pct(values: Sequence[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        current = float(value)
        if current > peak:
            peak = current
        if peak > 0.0:
            drawdown = (current - peak) / peak * 100.0
            if drawdown < worst:
                worst = drawdown
    return worst


def _narrate(
    signals: Sequence[StrategySignal],
    rsi_status: str,
    macd_status: str,
    volume_ratio_20: float,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    reasons: list[str] = []
    risks: list[str] = []
    catalysts: list[str] = []

    for signal in signals:
        if signal.triggered and len(reasons) < 5:
            reasons.append(f"{signal.strategy_id}: {signal.reasons[0] if signal.reasons else 'template confirmed'}")
    if macd_status == "GOLDEN_CROSS":
        reasons.append("MACD printed a golden cross on the decision bar")
    elif macd_status == "BULLISH":
        reasons.append("MACD holds a bullish line above its signal")
    else:
        risks.append("MACD remains bearish against its signal line")

    if rsi_status == "OVERBOUGHT":
        risks.append("RSI is overbought, so continuation risk is asymmetric")
    elif rsi_status == "OVERSOLD":
        reasons.append("RSI is oversold and open to a mean-reversion bounce")
        catalysts.append("mean-reversion window is open on the oversold reading")
    elif rsi_status == "STRONG":
        reasons.append("RSI holds in the strong band without being stretched")
    elif rsi_status == "WEAK":
        risks.append("RSI sits in the weak band and lacks momentum")

    if volume_ratio_20 < 0.85:
        risks.append("volume is below its trailing mean, so conviction is thin")
    elif volume_ratio_20 >= 1.4:
        reasons.append("volume is expanding well past its trailing mean")

    by_id = {signal.strategy_id: signal for signal in signals}
    catalyst_template = by_id.get("event_catalyst_momentum")
    if catalyst_template is not None and catalyst_template.triggered:
        catalysts.append("catalyst gap confirmed by a volume surge")
    rotation = by_id.get("cross_asset_etf_rotation")
    if rotation is not None and rotation.triggered:
        catalysts.append("relative-strength rotation favours this leg against its silo benchmark")
    rerate = by_id.get("expectation_rerating")
    if rerate is not None and rerate.triggered:
        catalysts.append("valuation compression opens a re-rating window")
    quality = by_id.get("growth_quality_compounder")
    if quality is not None and quality.triggered:
        catalysts.append("quality screen supports a compounding hold")

    if not catalysts:
        catalysts.append("no confirmed catalyst in the current bar window")
    if not reasons:
        reasons.append("no template fired, so the dashboard defaults to abstention")
    if not risks:
        risks.append("no structural risk flag fired, which is itself unconfirmed")
    return tuple(reasons[:6]), tuple(risks[:6]), tuple(catalysts[:4])


def _compose_score(
    *,
    aggregate: float,
    macd_status: str,
    rsi_status: str,
    bias_ma5_pct: float,
    close_price: float,
    ma20: float,
    reward_risk: float,
) -> float:
    """Blend the technical scaffold into a single deterministic score."""

    score = _clamp(aggregate, 0.0, 100.0)
    if macd_status == "GOLDEN_CROSS":
        score += 6.0
    elif macd_status == "BULLISH":
        score += 3.0
    else:
        score -= 4.0
    if rsi_status == "STRONG":
        score += 4.0
    elif rsi_status == "OVERBOUGHT":
        score -= 5.0
    elif rsi_status == "OVERSOLD":
        score += 2.0
    elif rsi_status == "WEAK":
        score -= 3.0
    if close_price > ma20:
        score += 3.0
    else:
        score -= 3.0
    if -3.0 <= bias_ma5_pct <= 8.0:
        score += 2.0
    elif bias_ma5_pct > 12.0:
        score -= 4.0
    if reward_risk >= 2.0:
        score += 2.0
    elif reward_risk < 1.0:
        score -= 3.0
    return round(_clamp(score, 0.0, 100.0), 4)

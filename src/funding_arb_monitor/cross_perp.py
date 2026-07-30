from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median

from funding_arb_monitor.cross_perp_venues import (
    ExternalPerpMarket,
    PerpBookQuote,
    PerpFundingEvent,
)


YEAR_MS = 365 * 86_400_000


@dataclass(frozen=True)
class CrossPerpConfig:
    notional_usd: float = 1_000.0
    history_days: int = 7
    min_history_coverage: float = 0.8
    max_basis_bps: float = 100.0
    max_quote_age_ms: int = 60_000
    continuity_window_ms: int = 90 * 60_000
    hyperliquid_fee_bps: float = 4.5


@dataclass(frozen=True)
class HyperliquidPerpMarket:
    dex: str
    asset: str
    current_funding_rate: float
    mark_price: float
    funding_captured_at_ms: int
    funding_events: tuple[PerpFundingEvent, ...]
    quote: PerpBookQuote


@dataclass(frozen=True)
class CrossPerpObservation:
    observed_at_ms: int
    hyperliquid_dex: str
    asset: str
    external_venue: str
    external_symbol: str
    direction: str
    hyperliquid_current_funding_rate: float | None
    external_current_funding_rate: float | None
    hyperliquid_funding_apr_pct: float | None
    external_funding_apr_pct: float | None
    gross_spread_apr_pct: float | None
    net_apr_7d_pct: float | None
    expected_funding_usd: float | None
    transaction_cost_usd: float | None
    basis_bps: float | None
    hyperliquid_mark_price: float | None
    external_mark_price: float | None
    hyperliquid_executable_price: float | None
    external_executable_price: float | None
    hyperliquid_slippage_bps: float | None
    external_slippage_bps: float | None
    hyperliquid_depth_usd: float
    external_depth_usd: float
    hyperliquid_fee_bps: float
    external_fee_bps: float
    hyperliquid_history_coverage: float
    external_history_coverage: float
    hyperliquid_funding_at_ms: int | None
    external_funding_at_ms: int | None
    hyperliquid_quote_at_ms: int | None
    external_quote_at_ms: int | None
    qualified: bool
    reasons: tuple[str, ...]
    streak: int = 0
    observation_ready: bool = False

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        return result


def realized_funding_apr(
    events: tuple[PerpFundingEvent, ...], *, window_days: int
) -> tuple[float | None, float, int | None]:
    timestamps = sorted({event.timestamp_ms for event in events})
    if len(timestamps) < 2:
        return None, 0.0, None

    interval_ms = int(
        median(later - earlier for earlier, later in zip(timestamps, timestamps[1:]))
    )
    if interval_ms <= 0:
        return None, 0.0, None

    expected_events = window_days * 86_400_000 / interval_ms
    coverage = min(len(events) / expected_events, 1.0)
    mean_funding_rate = sum(event.funding_rate for event in events) / len(events)
    apr = mean_funding_rate * YEAR_MS / interval_ms * 100
    return apr, coverage, interval_ms


def evaluate_direction(
    hyperliquid: HyperliquidPerpMarket,
    external: ExternalPerpMarket,
    direction: str,
    config: CrossPerpConfig,
    now_ms: int,
) -> CrossPerpObservation:
    if direction == "short_hyperliquid_long_external":
        hyperliquid_price = hyperliquid.quote.executable_sell_price
        external_price = external.quote.executable_buy_price
        hyperliquid_depth = hyperliquid.quote.bid_depth_usd
        external_depth = external.quote.ask_depth_usd
    elif direction == "long_hyperliquid_short_external":
        hyperliquid_price = hyperliquid.quote.executable_buy_price
        external_price = external.quote.executable_sell_price
        hyperliquid_depth = hyperliquid.quote.ask_depth_usd
        external_depth = external.quote.bid_depth_usd
    else:
        raise ValueError(f"unsupported cross-perp direction: {direction}")

    hyperliquid_apr, hyperliquid_coverage, hyperliquid_interval_ms = (
        realized_funding_apr(hyperliquid.funding_events, window_days=config.history_days)
    )
    external_apr, external_coverage, external_interval_ms = realized_funding_apr(
        external.funding_events, window_days=config.history_days
    )
    if hyperliquid_apr is None or external_apr is None:
        gross_apr_pct = None
        expected_funding_usd = None
    else:
        gross_apr_pct = (
            hyperliquid_apr - external_apr
            if direction == "short_hyperliquid_long_external"
            else external_apr - hyperliquid_apr
        )
        expected_funding_usd = (
            config.notional_usd
            * gross_apr_pct
            / 100
            * config.history_days
            / 365
        )

    hyperliquid_slippage_bps = _slippage_bps(
        hyperliquid_price, hyperliquid.mark_price
    )
    external_slippage_bps = _slippage_bps(external_price, external.mark_price)
    if hyperliquid_slippage_bps is None or external_slippage_bps is None:
        transaction_cost_usd = None
        net_apr_7d_pct = None
    else:
        transaction_cost_bps = 2 * (
            config.hyperliquid_fee_bps
            + external.quote.fee_bps
            + hyperliquid_slippage_bps
            + external_slippage_bps
        )
        transaction_cost_usd = config.notional_usd * transaction_cost_bps / 10_000
        net_apr_7d_pct = (
            None
            if expected_funding_usd is None
            else (expected_funding_usd - transaction_cost_usd)
            / config.notional_usd
            * 365
            / config.history_days
            * 100
        )

    basis_bps = (external.mark_price / hyperliquid.mark_price - 1) * 10_000
    reasons = _qualification_reasons(
        hyperliquid_coverage=hyperliquid_coverage,
        external_coverage=external_coverage,
        hyperliquid_latest_funding_at_ms=_latest_funding_at(hyperliquid.funding_events),
        external_latest_funding_at_ms=_latest_funding_at(external.funding_events),
        hyperliquid_interval_ms=hyperliquid_interval_ms,
        external_interval_ms=external_interval_ms,
        hyperliquid_quote_at_ms=hyperliquid.quote.captured_at_ms,
        external_quote_at_ms=external.quote.captured_at_ms,
        hyperliquid_price=hyperliquid_price,
        external_price=external_price,
        hyperliquid_depth_usd=hyperliquid_depth,
        external_depth_usd=external_depth,
        basis_bps=basis_bps,
        net_apr_7d_pct=net_apr_7d_pct,
        config=config,
        now_ms=now_ms,
    )
    return CrossPerpObservation(
        observed_at_ms=now_ms,
        hyperliquid_dex=hyperliquid.dex,
        asset=hyperliquid.asset,
        external_venue=external.instrument.venue,
        external_symbol=external.instrument.symbol,
        direction=direction,
        hyperliquid_current_funding_rate=hyperliquid.current_funding_rate,
        external_current_funding_rate=external.current_funding_rate,
        hyperliquid_funding_apr_pct=hyperliquid_apr,
        external_funding_apr_pct=external_apr,
        gross_spread_apr_pct=gross_apr_pct,
        net_apr_7d_pct=net_apr_7d_pct,
        expected_funding_usd=expected_funding_usd,
        transaction_cost_usd=transaction_cost_usd,
        basis_bps=basis_bps,
        hyperliquid_mark_price=hyperliquid.mark_price,
        external_mark_price=external.mark_price,
        hyperliquid_executable_price=hyperliquid_price,
        external_executable_price=external_price,
        hyperliquid_slippage_bps=hyperliquid_slippage_bps,
        external_slippage_bps=external_slippage_bps,
        hyperliquid_depth_usd=hyperliquid_depth,
        external_depth_usd=external_depth,
        hyperliquid_fee_bps=config.hyperliquid_fee_bps,
        external_fee_bps=external.quote.fee_bps,
        hyperliquid_history_coverage=hyperliquid_coverage,
        external_history_coverage=external_coverage,
        hyperliquid_funding_at_ms=hyperliquid.funding_captured_at_ms,
        external_funding_at_ms=external.funding_captured_at_ms,
        hyperliquid_quote_at_ms=hyperliquid.quote.captured_at_ms,
        external_quote_at_ms=external.quote.captured_at_ms,
        qualified=not reasons,
        reasons=reasons,
    )


def _slippage_bps(executable_price: float | None, mark_price: float) -> float | None:
    if executable_price is None:
        return None
    return abs(executable_price / mark_price - 1) * 10_000


def _latest_funding_at(events: tuple[PerpFundingEvent, ...]) -> int | None:
    return max((event.timestamp_ms for event in events), default=None)


def _qualification_reasons(
    *,
    hyperliquid_coverage: float,
    external_coverage: float,
    hyperliquid_latest_funding_at_ms: int | None,
    external_latest_funding_at_ms: int | None,
    hyperliquid_interval_ms: int | None,
    external_interval_ms: int | None,
    hyperliquid_quote_at_ms: int,
    external_quote_at_ms: int,
    hyperliquid_price: float | None,
    external_price: float | None,
    hyperliquid_depth_usd: float,
    external_depth_usd: float,
    basis_bps: float,
    net_apr_7d_pct: float | None,
    config: CrossPerpConfig,
    now_ms: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if (
        hyperliquid_coverage < config.min_history_coverage
        or external_coverage < config.min_history_coverage
    ):
        reasons.append("insufficient_history")
    if _is_stale_funding(
        hyperliquid_latest_funding_at_ms, hyperliquid_interval_ms, now_ms
    ) or _is_stale_funding(external_latest_funding_at_ms, external_interval_ms, now_ms):
        reasons.append("stale_funding")
    if (
        now_ms - hyperliquid_quote_at_ms > config.max_quote_age_ms
        or now_ms - external_quote_at_ms > config.max_quote_age_ms
    ):
        reasons.append("stale_quote")
    if (
        hyperliquid_price is None
        or external_price is None
        or hyperliquid_depth_usd < config.notional_usd
        or external_depth_usd < config.notional_usd
    ):
        reasons.append("insufficient_depth")
    if abs(basis_bps) > config.max_basis_bps:
        reasons.append("basis_too_wide")
    if net_apr_7d_pct is not None and net_apr_7d_pct <= 0:
        reasons.append("net_carry_non_positive")
    return tuple(reasons)


def _is_stale_funding(
    latest_funding_at_ms: int | None, interval_ms: int | None, now_ms: int
) -> bool:
    return (
        latest_funding_at_ms is not None
        and interval_ms is not None
        and now_ms - latest_funding_at_ms > 2 * interval_ms
    )

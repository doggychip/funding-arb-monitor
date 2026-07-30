from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from statistics import median
from typing import Callable

from funding_arb_monitor.cross_perp_venues import (
    ExternalPerpVenue,
    ExternalPerpMarket,
    PerpBookQuote,
    PerpFundingEvent,
    PerpInstrument,
)
from funding_arb_monitor.models import MarketSnapshot, PerpQuote
from funding_arb_monitor.store import Store


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


class CrossPerpMonitor:
    def __init__(
        self,
        hyperliquid: object,
        venues: list[ExternalPerpVenue],
        store: Store,
        *,
        config: CrossPerpConfig = CrossPerpConfig(),
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.hyperliquid = hyperliquid
        self.venues = venues
        self.store = store
        self.config = config
        self.now_ms = now_ms or (lambda: int(time.time() * 1_000))

    def run(self) -> dict[str, object]:
        run_id = self.store.start_cross_perp_run()
        venue_status: dict[str, str] = {}
        try:
            snapshots = self.hyperliquid.snapshots()
        except Exception as exc:
            self._finish_failed(run_id, venue_status, str(exc))
            raise RuntimeError("Hyperliquid market data unavailable") from exc

        catalogues: list[tuple[ExternalPerpVenue, dict[str, PerpInstrument]]] = []
        for venue in self.venues:
            try:
                instruments = venue.instruments()
            except Exception as exc:
                venue_status[venue.name] = f"failed: {exc}"
                continue
            venue_status[venue.name] = "success"
            catalogues.append((venue, instruments))

        if not catalogues:
            self._finish_failed(
                run_id, venue_status, "no external perpetual catalogues available"
            )
            raise RuntimeError("no external perpetual catalogues available")

        hyperliquid_markets: dict[tuple[str, str], HyperliquidPerpMarket] = {}
        external_jobs: list[
            tuple[
                MarketSnapshot,
                PerpInstrument,
                HyperliquidPerpMarket,
                Future[ExternalPerpMarket],
            ]
        ] = []
        match_count = 0
        executor = ThreadPoolExecutor(max_workers=8)
        try:
            for venue, instruments in catalogues:
                for snapshot in snapshots:
                    instrument = instruments.get(snapshot.coin)
                    if instrument is None:
                        continue
                    match_count += 1
                    market_key = (snapshot.dex, snapshot.coin)
                    hyperliquid_market = hyperliquid_markets.get(market_key)
                    if hyperliquid_market is None:
                        try:
                            hyperliquid_market = self._hyperliquid_market(snapshot)
                        except Exception as exc:
                            self._finish_failed(run_id, venue_status, str(exc))
                            raise RuntimeError(
                                "Hyperliquid market data unavailable"
                            ) from exc
                        hyperliquid_markets[market_key] = hyperliquid_market
                    future = executor.submit(
                        venue.market,
                        instrument,
                        days=self.config.history_days,
                        notional_usd=self.config.notional_usd,
                    )
                    external_jobs.append(
                        (snapshot, instrument, hyperliquid_market, future)
                    )
        except Exception:
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        executor.shutdown(wait=True)

        observations: list[CrossPerpObservation] = []
        for snapshot, instrument, hyperliquid_market, future in external_jobs:
            try:
                external_market = future.result()
                observations.extend(
                    evaluate_direction(
                        hyperliquid_market,
                        external_market,
                        direction,
                        self.config,
                        self.now_ms(),
                    )
                    for direction in (
                        "short_hyperliquid_long_external",
                        "long_hyperliquid_short_external",
                    )
                )
            except Exception:
                observations.extend(
                    self._venue_unavailable_observation(
                        snapshot, instrument, direction, hyperliquid_market
                    )
                    for direction in (
                        "short_hyperliquid_long_external",
                        "long_hyperliquid_short_external",
                    )
                )

        saved = self.store.save_cross_perp_observations(
            run_id,
            observations,
            continuity_window_ms=self.config.continuity_window_ms,
        )
        self.store.finish_cross_perp_run(
            run_id,
            status="success",
            venue_status=venue_status,
            match_count=match_count,
            evaluation_count=len(saved),
            positive_net_count=sum(
                item["net_apr_7d_pct"] is not None and item["net_apr_7d_pct"] > 0
                for item in saved
            ),
            ready_count=sum(bool(item["observation_ready"]) for item in saved),
        )
        return self.store.cross_perp_summary()

    def _finish_failed(
        self, run_id: int, venue_status: dict[str, str], error: str
    ) -> None:
        self.store.finish_cross_perp_run(
            run_id,
            status="failed",
            venue_status=venue_status,
            match_count=0,
            evaluation_count=0,
            positive_net_count=0,
            ready_count=0,
            error=error,
        )

    def _hyperliquid_market(self, snapshot: MarketSnapshot) -> HyperliquidPerpMarket:
        history = self.hyperliquid.funding_history(
            snapshot.coin, self.config.history_days
        )
        quote = self.hyperliquid.perp_quote(
            snapshot.coin, snapshot.dex, self.config.notional_usd
        )
        if quote is None:
            raise RuntimeError("Hyperliquid perpetual order book unavailable")
        return HyperliquidPerpMarket(
            dex=snapshot.dex,
            asset=snapshot.coin,
            current_funding_rate=snapshot.funding_rate,
            mark_price=snapshot.mark_price,
            funding_captured_at_ms=int(snapshot.captured_at.timestamp() * 1_000),
            funding_events=tuple(
                PerpFundingEvent(event.timestamp_ms, event.funding_rate)
                for event in history
            ),
            quote=_hyperliquid_book_quote(snapshot, quote, self.config.hyperliquid_fee_bps),
        )

    def _venue_unavailable_observation(
        self,
        snapshot: MarketSnapshot,
        instrument: PerpInstrument,
        direction: str,
        hyperliquid_market: HyperliquidPerpMarket,
    ) -> CrossPerpObservation:
        return CrossPerpObservation(
            observed_at_ms=self.now_ms(),
            hyperliquid_dex=snapshot.dex,
            asset=snapshot.coin,
            external_venue=instrument.venue,
            external_symbol=instrument.symbol,
            direction=direction,
            hyperliquid_current_funding_rate=hyperliquid_market.current_funding_rate,
            external_current_funding_rate=None,
            hyperliquid_funding_apr_pct=None,
            external_funding_apr_pct=None,
            gross_spread_apr_pct=None,
            net_apr_7d_pct=None,
            expected_funding_usd=None,
            transaction_cost_usd=None,
            basis_bps=None,
            hyperliquid_mark_price=hyperliquid_market.mark_price,
            external_mark_price=None,
            hyperliquid_executable_price=None,
            external_executable_price=None,
            hyperliquid_slippage_bps=None,
            external_slippage_bps=None,
            hyperliquid_depth_usd=0.0,
            external_depth_usd=0.0,
            hyperliquid_fee_bps=self.config.hyperliquid_fee_bps,
            external_fee_bps=0.0,
            hyperliquid_history_coverage=0.0,
            external_history_coverage=0.0,
            hyperliquid_funding_at_ms=hyperliquid_market.funding_captured_at_ms,
            external_funding_at_ms=None,
            hyperliquid_quote_at_ms=hyperliquid_market.quote.captured_at_ms,
            external_quote_at_ms=None,
            qualified=False,
            reasons=("venue_unavailable",),
        )


def _hyperliquid_book_quote(
    snapshot: MarketSnapshot, quote: PerpQuote, fee_bps: float
) -> PerpBookQuote:
    return PerpBookQuote(
        venue="hyperliquid",
        asset=snapshot.coin,
        symbol=snapshot.coin,
        bid=quote.bid,
        ask=quote.ask,
        executable_buy_price=quote.executable_buy_price,
        executable_sell_price=quote.executable_sell_price,
        bid_depth_usd=quote.bid_depth_usd,
        ask_depth_usd=quote.ask_depth_usd,
        fee_bps=fee_bps,
        captured_at_ms=quote.captured_at_ms,
    )


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

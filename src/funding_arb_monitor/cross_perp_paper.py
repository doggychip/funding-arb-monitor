from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from .alerts import (
    render_cross_perp_shadow_entry,
    render_cross_perp_shadow_exit,
    send_discord_alert,
)
from .cross_perp import (
    CrossPerpConfig,
    CrossPerpObservation,
    HyperliquidPerpMarket,
    _hyperliquid_book_quote,
    evaluate_direction,
)
from .cross_perp_venues import (
    ExternalPerpMarket,
    ExternalPerpVenue,
    PerpInstrument,
    PerpFundingEvent,
)
from .store import Store


Route = dict[str, object]
HOUR_MS = 3_600_000


@dataclass(frozen=True)
class ExecutionTruthConfig:
    forward_horizon_hours: int = 24
    min_depth_multiple: float = 3.0
    capacity_notionals_usd: tuple[float, ...] = (250.0, 500.0, 1_000.0, 2_000.0)
    delayed_leg_ms: tuple[int, ...] = (100, 250, 500)
    max_delayed_leg_adverse_bps: float = 10.0
    stressed_funding_capture: float = 0.5
    leverage: float = 2.0
    maintenance_margin_pct: float = 5.0
    min_liquidation_buffer_pct: float = 20.0
    economic_exit_scans: int = 3
    reentry_cooldown_ms: int = 24 * HOUR_MS


@dataclass(frozen=True)
class DelayedLegSample:
    delay_ms: int
    hyperliquid_price: float | None
    external_price: float | None
    worst_adverse_bps: float | None
    passed: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionTruthEvidence:
    forward_horizon_hours: int
    hyperliquid_settlements: int
    external_settlements: int
    forward_funding_usd: float | None
    forward_net_profit_usd: float | None
    stressed_forward_net_profit_usd: float | None
    depth_multiple: float
    capacity_curve: tuple[dict[str, object], ...]
    delayed_leg_samples: tuple[DelayedLegSample, ...]
    worst_delayed_leg_adverse_bps: float | None
    initial_margin_usd: float
    reserve_usd: float
    committed_capital_usd: float
    liquidation_buffer_pct: float
    return_on_capital_24h_pct: float | None
    paper_executable: bool
    readiness_level: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["capacity_curve"] = list(self.capacity_curve)
        result["delayed_leg_samples"] = [
            sample.as_dict() for sample in self.delayed_leg_samples
        ]
        result["execution_reasons"] = list(self.reasons)
        result["forward_funding_usd_24h"] = result.pop("forward_funding_usd")
        result["forward_net_profit_usd_24h"] = result.pop(
            "forward_net_profit_usd"
        )
        result["stressed_forward_net_profit_usd_24h"] = result.pop(
            "stressed_forward_net_profit_usd"
        )
        result.pop("reasons")
        return result


def evaluate_execution_truth(
    observation: CrossPerpObservation,
    *,
    hyperliquid_next_funding_at_ms: int | None,
    hyperliquid_funding_interval_ms: int | None,
    external_next_funding_at_ms: int | None,
    external_funding_interval_ms: int | None,
    delayed_leg_samples: tuple[DelayedLegSample, ...],
    capacity_curve: tuple[dict[str, object], ...] | None = None,
    notional_usd: float = 1_000.0,
    config: ExecutionTruthConfig = ExecutionTruthConfig(),
) -> ExecutionTruthEvidence:
    horizon_end_ms = observation.observed_at_ms + config.forward_horizon_hours * HOUR_MS
    hyperliquid_settlements = _settlement_count(
        hyperliquid_next_funding_at_ms,
        hyperliquid_funding_interval_ms,
        observation.observed_at_ms,
        horizon_end_ms,
    )
    external_settlements = _settlement_count(
        external_next_funding_at_ms,
        external_funding_interval_ms,
        observation.observed_at_ms,
        horizon_end_ms,
    )
    hyperliquid_sign = (
        1 if observation.direction == "short_hyperliquid_long_external" else -1
    )
    external_sign = -hyperliquid_sign
    rates_available = (
        observation.hyperliquid_current_funding_rate is not None
        and observation.external_current_funding_rate is not None
        and hyperliquid_settlements > 0
        and external_settlements > 0
    )
    forward_funding_usd = None
    if rates_available:
        forward_funding_usd = notional_usd * (
            float(observation.hyperliquid_current_funding_rate)
            * hyperliquid_sign
            * hyperliquid_settlements
            + float(observation.external_current_funding_rate)
            * external_sign
            * external_settlements
        )
    transaction_cost = observation.transaction_cost_usd
    forward_net = (
        None
        if forward_funding_usd is None or transaction_cost is None
        else forward_funding_usd - transaction_cost
    )
    stressed_forward_net = (
        None
        if forward_funding_usd is None or transaction_cost is None
        else forward_funding_usd * config.stressed_funding_capture
        - transaction_cost
        - notional_usd * 10.0 / 10_000
    )
    depth_multiple = min(
        observation.hyperliquid_depth_usd,
        observation.external_depth_usd,
    ) / notional_usd
    if capacity_curve is None:
        capacity_curve = tuple(
            {
                "notional_usd": notional,
                "observed_depth_multiple": min(
                    observation.hyperliquid_depth_usd,
                    observation.external_depth_usd,
                )
                / notional,
                "has_executable_depth": min(
                    observation.hyperliquid_depth_usd,
                    observation.external_depth_usd,
                )
                >= notional * config.min_depth_multiple,
            }
            for notional in config.capacity_notionals_usd
        )
    primary_capacity = min(
        capacity_curve,
        key=lambda row: abs(float(row["notional_usd"]) - notional_usd),
    )
    depth_multiple = float(primary_capacity["observed_depth_multiple"])
    worst_delayed = max(
        (
            float(sample.worst_adverse_bps)
            for sample in delayed_leg_samples
            if sample.worst_adverse_bps is not None
        ),
        default=None,
    )
    initial_margin = 2 * notional_usd / config.leverage
    reserve = float(transaction_cost or 0) + notional_usd * 10.0 / 10_000
    committed_capital = initial_margin + reserve
    liquidation_buffer_pct = 100 / config.leverage - config.maintenance_margin_pct
    return_on_capital = (
        None
        if forward_net is None
        else forward_net / committed_capital * 100
    )

    reasons = list(observation.reasons)
    if not rates_available:
        reasons.append("forward_funding_schedule_unavailable")
    if forward_net is None or forward_net <= 0:
        reasons.append("forward_net_non_positive")
    if stressed_forward_net is None or stressed_forward_net <= 0:
        reasons.append("stressed_forward_net_non_positive")
    if not bool(primary_capacity["has_executable_depth"]):
        reasons.append("depth_below_3x_notional")
    if not delayed_leg_samples:
        reasons.append("delayed_leg_evidence_unavailable")
    elif any(not sample.passed for sample in delayed_leg_samples):
        reasons.append("delayed_leg_stress_failed")
    if liquidation_buffer_pct < config.min_liquidation_buffer_pct:
        reasons.append("liquidation_buffer_too_thin")
    reasons = list(dict.fromkeys(reasons))
    paper_executable = bool(observation.qualified and not reasons)
    return ExecutionTruthEvidence(
        forward_horizon_hours=config.forward_horizon_hours,
        hyperliquid_settlements=hyperliquid_settlements,
        external_settlements=external_settlements,
        forward_funding_usd=forward_funding_usd,
        forward_net_profit_usd=forward_net,
        stressed_forward_net_profit_usd=stressed_forward_net,
        depth_multiple=depth_multiple,
        capacity_curve=capacity_curve,
        delayed_leg_samples=delayed_leg_samples,
        worst_delayed_leg_adverse_bps=worst_delayed,
        initial_margin_usd=initial_margin,
        reserve_usd=reserve,
        committed_capital_usd=committed_capital,
        liquidation_buffer_pct=liquidation_buffer_pct,
        return_on_capital_24h_pct=return_on_capital,
        paper_executable=paper_executable,
        readiness_level="paper_executable" if paper_executable else "preflight_failed",
        reasons=tuple(reasons),
    )


def _settlement_count(
    next_at_ms: int | None,
    interval_ms: int | None,
    start_ms: int,
    end_ms: int,
) -> int:
    if next_at_ms is None or interval_ms is None or interval_ms <= 0:
        return 0
    next_at_ms = max(next_at_ms, start_ms)
    if next_at_ms > end_ms:
        return 0
    return 1 + (end_ms - next_at_ms) // interval_ms


def _worst_delayed_leg_adverse_bps(
    direction: str,
    baseline_hyperliquid: float | None,
    baseline_external: float | None,
    delayed_hyperliquid: float | None,
    delayed_external: float | None,
) -> float | None:
    prices = (
        baseline_hyperliquid,
        baseline_external,
        delayed_hyperliquid,
        delayed_external,
    )
    if any(price is None or price <= 0 for price in prices):
        return None
    if direction == "short_hyperliquid_long_external":
        hyperliquid_adverse = (
            float(baseline_hyperliquid) / float(delayed_hyperliquid) - 1
        ) * 10_000
        external_adverse = (
            float(delayed_external) / float(baseline_external) - 1
        ) * 10_000
    else:
        hyperliquid_adverse = (
            float(delayed_hyperliquid) / float(baseline_hyperliquid) - 1
        ) * 10_000
        external_adverse = (
            float(baseline_external) / float(delayed_external) - 1
        ) * 10_000
    return max(0.0, hyperliquid_adverse, external_adverse)


@dataclass(frozen=True)
class CrossPerpPreflightEvidence:
    observation: CrossPerpObservation
    hyperliquid_exit_price: float | None
    external_exit_price: float | None
    hyperliquid_funding_events: tuple[PerpFundingEvent, ...]
    external_funding_events: tuple[PerpFundingEvent, ...]
    execution_truth: ExecutionTruthEvidence | None = None

    def as_dict(self, notional_usd: float) -> dict[str, object]:
        result = {
            **self.observation.as_dict(),
            "notional_usd": notional_usd,
            "hyperliquid_exit_price": self.hyperliquid_exit_price,
            "external_exit_price": self.external_exit_price,
        }
        if self.execution_truth is not None:
            result.update(self.execution_truth.as_dict())
        else:
            result.update(
                {
                    "paper_executable": False,
                    "readiness_level": "preflight_failed",
                    "execution_reasons": ["execution_truth_unavailable"],
                }
            )
        return result


class CrossPerpPreflight(Protocol):
    def refresh(self, route: Route) -> CrossPerpPreflightEvidence: ...


class PublicCrossPerpPreflight:
    """Second public-data quote pass used only for paper evidence."""

    def __init__(
        self,
        hyperliquid: object,
        venues: list[ExternalPerpVenue],
        *,
        config: CrossPerpConfig = CrossPerpConfig(),
        execution_config: ExecutionTruthConfig = ExecutionTruthConfig(),
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.hyperliquid = hyperliquid
        self.venues = {venue.name: venue for venue in venues}
        self.config = config
        self.execution_config = execution_config
        self.now_ms = now_ms or (lambda: int(time.time() * 1_000))
        self.sleep = sleep
        self._snapshots: dict[tuple[str, str], object] | None = None
        self._instruments: dict[str, dict[str, object]] = {}

    def refresh(self, route: Route) -> CrossPerpPreflightEvidence:
        if self._snapshots is None:
            snapshots = self.hyperliquid.snapshots()
            if not snapshots:
                raise RuntimeError("empty Hyperliquid market catalogue")
            self._snapshots = {
                (str(snapshot.dex), str(snapshot.coin)): snapshot
                for snapshot in snapshots
            }
        market_key = (str(route["hyperliquid_dex"]), str(route["asset"]))
        snapshot = self._snapshots.get(market_key)
        if snapshot is None:
            raise RuntimeError("Hyperliquid route is no longer available")

        quote_reader = getattr(self.hyperliquid, "perp_book_quote", None)
        if quote_reader is None:
            quote_reader = self.hyperliquid.perp_quote
        quote = quote_reader(
            str(route["asset"]),
            str(route["hyperliquid_dex"]),
            self.config.notional_usd,
        )
        if quote is None:
            raise RuntimeError("Hyperliquid perpetual order book unavailable")
        history = self.hyperliquid.funding_history(
            str(route["asset"]), self.config.history_days
        )
        hyperliquid = HyperliquidPerpMarket(
            dex=str(snapshot.dex),
            asset=str(snapshot.coin),
            current_funding_rate=float(snapshot.funding_rate),
            mark_price=float(snapshot.mark_price),
            mark_captured_at_ms=int(snapshot.captured_at.timestamp() * 1_000),
            funding_captured_at_ms=int(snapshot.captured_at.timestamp() * 1_000),
            funding_events=tuple(
                PerpFundingEvent(int(item.timestamp_ms), float(item.funding_rate))
                for item in history
            ),
            quote=_hyperliquid_book_quote(
                snapshot, quote, self.config.hyperliquid_fee_bps
            ),
        )

        venue_name = str(route["external_venue"])
        venue = self.venues.get(venue_name)
        if venue is None:
            raise RuntimeError("external perpetual venue is unavailable")
        if venue_name not in self._instruments:
            self._instruments[venue_name] = venue.instruments()
        instrument = self._instruments[venue_name].get(str(route["asset"]))
        if instrument is None or str(instrument.symbol) != str(route["external_symbol"]):
            raise RuntimeError("exact external perpetual route is unavailable")
        external = venue.market(
            instrument,
            days=self.config.history_days,
            notional_usd=self.config.notional_usd,
        )
        observation = evaluate_direction(
            hyperliquid,
            external,
            str(route["direction"]),
            self.config,
            self.now_ms(),
        )
        hyperliquid_exit_price, external_exit_price = self._exit_prices(
            observation.direction, hyperliquid, external
        )
        delayed_leg_samples = self._delayed_leg_samples(
            route,
            venue,
            instrument,
            observation,
        )
        capacity_curve = self._capacity_curve(route, venue, instrument, observation)
        execution_truth = evaluate_execution_truth(
            observation,
            hyperliquid_next_funding_at_ms=(
                (observation.observed_at_ms // HOUR_MS + 1) * HOUR_MS
            ),
            hyperliquid_funding_interval_ms=HOUR_MS,
            external_next_funding_at_ms=external.next_funding_at_ms,
            external_funding_interval_ms=external.funding_interval_ms,
            delayed_leg_samples=delayed_leg_samples,
            capacity_curve=capacity_curve,
            notional_usd=self.config.notional_usd,
            config=self.execution_config,
        )
        return CrossPerpPreflightEvidence(
            observation=observation,
            hyperliquid_exit_price=hyperliquid_exit_price,
            external_exit_price=external_exit_price,
            hyperliquid_funding_events=hyperliquid.funding_events,
            external_funding_events=external.funding_events,
            execution_truth=execution_truth,
        )

    def _delayed_leg_samples(
        self,
        route: Route,
        venue: ExternalPerpVenue,
        instrument: PerpInstrument,
        observation: CrossPerpObservation,
    ) -> tuple[DelayedLegSample, ...]:
        quote_reader = getattr(self.hyperliquid, "perp_book_quote", None)
        if quote_reader is None:
            quote_reader = self.hyperliquid.perp_quote
        samples: list[DelayedLegSample] = []
        for delay_ms in self.execution_config.delayed_leg_ms:
            self.sleep(delay_ms / 1_000)
            hyperliquid_quote = quote_reader(
                str(route["asset"]),
                str(route["hyperliquid_dex"]),
                self.config.notional_usd,
            )
            external_quote = venue.quote(
                instrument,
                notional_usd=self.config.notional_usd,
            )
            hyperliquid_price, external_price = self._entry_prices(
                observation.direction,
                hyperliquid_quote,
                external_quote,
            )
            adverse_bps = _worst_delayed_leg_adverse_bps(
                observation.direction,
                observation.hyperliquid_executable_price,
                observation.external_executable_price,
                hyperliquid_price,
                external_price,
            )
            samples.append(
                DelayedLegSample(
                    delay_ms=delay_ms,
                    hyperliquid_price=hyperliquid_price,
                    external_price=external_price,
                    worst_adverse_bps=adverse_bps,
                    passed=bool(
                        adverse_bps is not None
                        and adverse_bps
                        <= self.execution_config.max_delayed_leg_adverse_bps
                    ),
                )
            )
        return tuple(samples)

    def _capacity_curve(
        self,
        route: Route,
        venue: ExternalPerpVenue,
        instrument: PerpInstrument,
        observation: CrossPerpObservation,
    ) -> tuple[dict[str, object], ...]:
        quote_reader = getattr(self.hyperliquid, "perp_book_quote", None)
        if quote_reader is None:
            quote_reader = self.hyperliquid.perp_quote
        rows: list[dict[str, object]] = []
        for notional in self.execution_config.capacity_notionals_usd:
            hyperliquid_quote = quote_reader(
                str(route["asset"]), str(route["hyperliquid_dex"]), notional
            )
            external_quote = venue.quote(instrument, notional_usd=notional)
            hyperliquid_price, external_price = self._entry_prices(
                observation.direction,
                hyperliquid_quote,
                external_quote,
            )
            if observation.direction == "short_hyperliquid_long_external":
                hyperliquid_depth = hyperliquid_quote.bid_depth_usd
                external_depth = external_quote.ask_depth_usd
            else:
                hyperliquid_depth = hyperliquid_quote.ask_depth_usd
                external_depth = external_quote.bid_depth_usd
            depth_multiple = min(hyperliquid_depth, external_depth) / notional
            combined_slippage_bps = (
                None
                if hyperliquid_price is None or external_price is None
                else abs(
                    hyperliquid_price
                    / ((hyperliquid_quote.bid + hyperliquid_quote.ask) / 2)
                    - 1
                )
                * 10_000
                + abs(external_price / external_quote.mid - 1) * 10_000
            )
            rows.append(
                {
                    "notional_usd": notional,
                    "hyperliquid_executable_price": hyperliquid_price,
                    "external_executable_price": external_price,
                    "combined_entry_slippage_bps": combined_slippage_bps,
                    "observed_depth_multiple": depth_multiple,
                    "has_executable_depth": bool(
                        hyperliquid_price is not None
                        and external_price is not None
                        and depth_multiple >= self.execution_config.min_depth_multiple
                    ),
                }
            )
        return tuple(rows)

    @staticmethod
    def _entry_prices(
        direction: str,
        hyperliquid_quote: object,
        external_quote: object,
    ) -> tuple[float | None, float | None]:
        if direction == "short_hyperliquid_long_external":
            return (
                hyperliquid_quote.executable_sell_price,
                external_quote.executable_buy_price,
            )
        return (
            hyperliquid_quote.executable_buy_price,
            external_quote.executable_sell_price,
        )

    @staticmethod
    def _exit_prices(
        direction: str,
        hyperliquid: HyperliquidPerpMarket,
        external: ExternalPerpMarket,
    ) -> tuple[float | None, float | None]:
        if direction == "short_hyperliquid_long_external":
            return (
                hyperliquid.quote.executable_buy_price,
                external.quote.executable_sell_price,
            )
        return (
            hyperliquid.quote.executable_sell_price,
            external.quote.executable_buy_price,
        )


class CrossPerpShadowEngine:
    """Maintain simulated two-perpetual positions; never submits orders."""

    def __init__(
        self,
        store: Store,
        preflight: CrossPerpPreflight,
        *,
        notional_usd: float = 1_000.0,
        max_open_positions: int = 3,
        max_holding_days: int = 7,
        execution_config: ExecutionTruthConfig = ExecutionTruthConfig(),
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.preflight = preflight
        self.notional_usd = notional_usd
        self.max_open_positions = max_open_positions
        self.max_holding_ms = max_holding_days * 86_400_000
        self.execution_config = execution_config
        self.now_ms = now_ms or (lambda: int(time.time() * 1_000))

    def run(self) -> dict[str, int]:
        current = self.store.latest_cross_perp_observations(500)
        current_by_route = {self._route_key(item): item for item in current}
        open_positions = self.store.open_cross_perp_paper_positions()
        ready = [item for item in current if bool(item["observation_ready"])]
        routes: dict[tuple[object, ...], Route] = {
            self._route_key(item): item for item in ready
        }
        for position in open_positions:
            route = current_by_route.get(self._route_key(position))
            if route is not None:
                routes[self._route_key(route)] = route

        refreshed: dict[tuple[object, ...], CrossPerpPreflightEvidence] = {}
        failures: dict[tuple[object, ...], str] = {}
        for route_key, route in routes.items():
            try:
                refreshed[route_key] = self.preflight.refresh(route)
            except Exception as exc:
                failures[route_key] = f"{type(exc).__name__}: {exc}"[:500]

        opened = 0
        checks_passed = 0
        open_route_keys = {self._route_key(item) for item in open_positions}
        for route in ready:
            route_key = self._route_key(route)
            evidence = refreshed.get(route_key)
            checked_at_ms = self.now_ms()
            truth = evidence.execution_truth if evidence is not None else None
            reasons = (
                list(truth.reasons)
                if truth is not None
                else ["preflight_unavailable"]
            )
            payload = (
                evidence.as_dict(self.notional_usd)
                if evidence is not None
                else {"error": failures.get(route_key, "preflight unavailable")}
            )
            passed = bool(truth is not None and truth.paper_executable)
            cooldown_until_ms = self.store.cross_perp_route_cooldown_until(
                route,
                self.execution_config.reentry_cooldown_ms,
            )
            if cooldown_until_ms is not None and cooldown_until_ms > checked_at_ms:
                passed = False
                reasons = [*reasons, "route_cooldown"]
                payload["cooldown_until_ms"] = cooldown_until_ms
            check_id = self.store.save_cross_perp_entry_check(
                source_run_id=int(route["run_id"]),
                route=route,
                status="passed" if passed else "failed",
                reasons=reasons,
                payload=payload,
                checked_at_ms=checked_at_ms,
            )
            checks_passed += int(passed)
            if (
                not passed
                or route_key in open_route_keys
                or len(open_positions) + opened >= self.max_open_positions
            ):
                continue
            position_id = self.store.open_cross_perp_paper_position(
                entry_check_id=check_id,
                source_run_id=int(route["run_id"]),
                evidence=payload,
                opened_at_ms=checked_at_ms,
            )
            position = next(
                item
                for item in self.store.open_cross_perp_paper_positions()
                if int(item["id"]) == position_id
            )
            send_discord_alert(
                render_cross_perp_shadow_entry(position),
                store=self.store,
                event_type="cross_perp_shadow_opened",
            )
            open_route_keys.add(route_key)
            opened += 1

        updated = 0
        closed = 0
        for position in self.store.open_cross_perp_paper_positions():
            route_key = self._route_key(position)
            evidence = refreshed.get(route_key)
            route = current_by_route.get(route_key)
            if evidence is None or route is None:
                continue
            if not self._record_position_evidence(position, evidence):
                continue
            updated += 1
            reason = self._exit_reason(position, route, evidence)
            if reason is None:
                continue
            exit_fee = float(position["notional_usd"]) * (
                float(evidence.observation.hyperliquid_fee_bps)
                + float(evidence.observation.external_fee_bps)
            ) / 10_000
            self.store.close_cross_perp_paper_position(
                int(position["id"]),
                closed_at_ms=self.now_ms(),
                reason=reason,
                hyperliquid_exit_price=float(evidence.hyperliquid_exit_price),
                external_exit_price=float(evidence.external_exit_price),
                exit_fee_usd=exit_fee,
            )
            closed_position = next(
                item
                for item in self.store.cross_perp_paper_positions()
                if int(item["id"]) == int(position["id"])
            )
            send_discord_alert(
                render_cross_perp_shadow_exit(closed_position),
                store=self.store,
                event_type="cross_perp_shadow_closed",
            )
            closed += 1
        return {
            "ready_routes": len(ready),
            "checks_passed": checks_passed,
            "opened": opened,
            "updated": updated,
            "closed": closed,
        }

    def _record_position_evidence(
        self,
        position: dict[str, object],
        evidence: CrossPerpPreflightEvidence,
    ) -> bool:
        if (
            evidence.hyperliquid_exit_price is None
            or evidence.external_exit_price is None
        ):
            return False
        opened_at_ms = int(position["opened_at_ms"])
        direction = str(position["direction"])
        hyperliquid_sign = (
            1 if direction == "short_hyperliquid_long_external" else -1
        )
        external_sign = -hyperliquid_sign
        entries = [
            (
                "hyperliquid",
                event.timestamp_ms,
                event.funding_rate,
                float(position["notional_usd"])
                * event.funding_rate
                * hyperliquid_sign,
            )
            for event in evidence.hyperliquid_funding_events
            if event.timestamp_ms > opened_at_ms
        ] + [
            (
                str(position["external_venue"]),
                event.timestamp_ms,
                event.funding_rate,
                float(position["notional_usd"])
                * event.funding_rate
                * external_sign,
            )
            for event in evidence.external_funding_events
            if event.timestamp_ms > opened_at_ms
        ]
        self.store.save_cross_perp_paper_accruals(int(position["id"]), entries)
        if direction == "short_hyperliquid_long_external":
            hyperliquid_pnl = (
                float(position["hyperliquid_entry_price"])
                - evidence.hyperliquid_exit_price
            ) * float(position["hyperliquid_quantity"])
            external_pnl = (
                evidence.external_exit_price
                - float(position["external_entry_price"])
            ) * float(position["external_quantity"])
        else:
            hyperliquid_pnl = (
                evidence.hyperliquid_exit_price
                - float(position["hyperliquid_entry_price"])
            ) * float(position["hyperliquid_quantity"])
            external_pnl = (
                float(position["external_entry_price"])
                - evidence.external_exit_price
            ) * float(position["external_quantity"])
        self.store.save_cross_perp_paper_mark(
            int(position["id"]),
            timestamp_ms=evidence.observation.observed_at_ms,
            hyperliquid_exit_price=evidence.hyperliquid_exit_price,
            external_exit_price=evidence.external_exit_price,
            hyperliquid_pnl_usd=hyperliquid_pnl,
            external_pnl_usd=external_pnl,
            basis_bps=float(evidence.observation.basis_bps or 0),
            net_apr_7d_pct=evidence.observation.net_apr_7d_pct,
            qualified=bool(
                evidence.execution_truth is not None
                and evidence.execution_truth.paper_executable
            ),
            reasons=(
                list(evidence.execution_truth.reasons)
                if evidence.execution_truth is not None
                else ["execution_truth_unavailable"]
            ),
        )
        return True

    def _exit_reason(
        self,
        position: dict[str, object],
        route: Route,
        evidence: CrossPerpPreflightEvidence,
    ) -> str | None:
        if self.now_ms() - int(position["opened_at_ms"]) >= self.max_holding_ms:
            return "maximum_7d_holding_period"
        reasons = set(
            evidence.execution_truth.reasons
            if evidence.execution_truth is not None
            else ("execution_truth_unavailable",)
        )
        hard_risk = reasons.intersection(
            {
                "stale_mark",
                "stale_quote",
                "insufficient_depth",
                "basis_too_wide",
                "liquidation_buffer_too_thin",
            }
        )
        if hard_risk:
            return f"hard_risk_{sorted(hard_risk)[0]}"
        if (
            not bool(route["observation_ready"])
            or evidence.execution_truth is None
            or not evidence.execution_truth.paper_executable
        ) and self.store.cross_perp_paper_failure_streak(int(position["id"])) >= (
            self.execution_config.economic_exit_scans
        ):
            return "economic_deterioration_3_scans"
        return None

    @staticmethod
    def _route_key(route: Route) -> tuple[object, ...]:
        return (
            route["hyperliquid_dex"],
            route["asset"],
            route["external_venue"],
            route["external_symbol"],
            route["direction"],
        )

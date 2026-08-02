from __future__ import annotations

import time
from dataclasses import dataclass
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
    PerpFundingEvent,
)
from .store import Store


Route = dict[str, object]


@dataclass(frozen=True)
class CrossPerpPreflightEvidence:
    observation: CrossPerpObservation
    hyperliquid_exit_price: float | None
    external_exit_price: float | None
    hyperliquid_funding_events: tuple[PerpFundingEvent, ...]
    external_funding_events: tuple[PerpFundingEvent, ...]

    def as_dict(self, notional_usd: float) -> dict[str, object]:
        return {
            **self.observation.as_dict(),
            "notional_usd": notional_usd,
            "hyperliquid_exit_price": self.hyperliquid_exit_price,
            "external_exit_price": self.external_exit_price,
        }


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
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.hyperliquid = hyperliquid
        self.venues = {venue.name: venue for venue in venues}
        self.config = config
        self.now_ms = now_ms or (lambda: int(time.time() * 1_000))
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
        return CrossPerpPreflightEvidence(
            observation=observation,
            hyperliquid_exit_price=hyperliquid_exit_price,
            external_exit_price=external_exit_price,
            hyperliquid_funding_events=hyperliquid.funding_events,
            external_funding_events=external.funding_events,
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
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.preflight = preflight
        self.notional_usd = notional_usd
        self.max_open_positions = max_open_positions
        self.max_holding_ms = max_holding_days * 86_400_000
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
            reasons = (
                list(evidence.observation.reasons)
                if evidence is not None
                else ["preflight_unavailable"]
            )
            payload = (
                evidence.as_dict(self.notional_usd)
                if evidence is not None
                else {"error": failures.get(route_key, "preflight unavailable")}
            )
            passed = bool(evidence is not None and evidence.observation.qualified)
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
            qualified=evidence.observation.qualified,
            reasons=list(evidence.observation.reasons),
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
        if not bool(route["observation_ready"]):
            return "observation_readiness_lost"
        if not evidence.observation.qualified:
            return "preflight_no_longer_qualified"
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

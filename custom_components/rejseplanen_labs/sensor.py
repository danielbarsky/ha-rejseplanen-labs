"""Sensors for Rejseplanen Labs: an aggregate nearby board + fixed stops.

Rendering is deliberately decoupled from fetching. Departure times are absolute
timestamps, so the "leaves in N minutes" view can be recomputed locally from
already-cached data. A LOCAL_TICK timer does exactly that, which is what makes
a slow -- or entirely on-demand -- update interval usable: the countdown keeps
ticking and departed services drop off without spending a single API call.
Only real-time delay revisions need an actual fetch.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import RejseplanenConfigEntry
from .api import Departure, StopDepartures
from .const import (
    ATTR_ATTRIBUTION,
    DATA_CENTER,
    DATA_FIXED,
    DATA_NEARBY,
    DEPARTED_GRACE,
    DOMAIN,
    LOCAL_TICK,
)
from .coordinator import RejseplanenCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RejseplanenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the aggregate sensor and any fixed-stop sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [RejseplanenNearbySensor(coordinator, entry)]

    fixed: dict[str, StopDepartures] = coordinator.data.get(DATA_FIXED, {})
    entities.extend(
        RejseplanenStopSensor(coordinator, entry, stop_id)
        for stop_id in fixed
    )
    async_add_entities(entities)


def _is_upcoming(dep: Departure) -> bool:
    """Has this service not left yet (within a grace period)?

    Between fetches the cached board ages. Filtering on read means a stale board
    degrades honestly -- it shows fewer departures rather than departures that
    already went.
    """
    when = dep.when
    return when is not None and when > dt_util.now() - DEPARTED_GRACE


def _departure_dict(dep: Departure) -> dict:
    when = dep.when
    minutes = None
    if when is not None:
        # `when` is tz-aware (Europe/Copenhagen); compare against aware now.
        minutes = max(0, round((when - dt_util.now()).total_seconds() / 60))
    return {
        "name": dep.name,
        "type": dep.type,
        "line": dep.line,
        "operator": dep.operator,
        "direction": dep.direction,
        "scheduled": dep.scheduled.isoformat() if dep.scheduled else None,
        "realtime": dep.realtime.isoformat() if dep.realtime else None,
        "in_minutes": minutes,
        "is_realtime": dep.is_realtime,
        "cancelled": dep.cancelled,
        "track": dep.track,
    }


class _BaseSensor(CoordinatorEntity[RejseplanenCoordinator], SensorEntity):
    """Shared plumbing, including the local re-render tick."""

    _attr_has_entity_name = True
    _attr_attribution = ATTR_ATTRIBUTION
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coordinator: RejseplanenCoordinator, entry: RejseplanenConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._last_render: tuple | None = None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Rejseplanen Labs",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._handle_local_tick, LOCAL_TICK)
        )

    @callback
    def _handle_local_tick(self, _now) -> None:
        """Re-render from cached data; no API traffic.

        Skips the write when nothing visible changed, so the tick doesn't fill
        the recorder with identical states -- in practice this writes about
        once a minute, as the countdown rolls over.
        """
        render = self._render_signature()
        if render == self._last_render:
            return
        self._last_render = render
        self.async_write_ha_state()

    def _render_signature(self) -> tuple:
        """Cheap fingerprint of what the user would actually see."""
        departures = self.extra_state_attributes.get("departures") or []
        return (
            self.native_value,
            tuple(dep.get("in_minutes") for dep in departures),
        )


class RejseplanenNearbySensor(_BaseSensor):
    """Aggregate board across every stop within the radius.

    State is the timestamp of the very next departure anywhere in range;
    attributes carry the full merged, time-sorted list and per-stop breakdown.
    """

    _attr_icon = "mdi:map-marker-radius"
    _attr_translation_key = "nearby"

    def __init__(
        self, coordinator: RejseplanenCoordinator, entry: RejseplanenConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_nearby"

    @property
    def _all_departures(self) -> list[tuple[StopDepartures, Departure]]:
        pairs: list[tuple[StopDepartures, Departure]] = []
        for stop in self.coordinator.data.get(DATA_NEARBY, []):
            for dep in stop.departures:
                if _is_upcoming(dep):
                    pairs.append((stop, dep))
        pairs.sort(key=lambda pair: pair[1].when)
        return pairs

    @property
    def native_value(self) -> datetime | None:
        pairs = self._all_departures
        return pairs[0][1].when if pairs else None

    @property
    def extra_state_attributes(self) -> dict:
        stops = self.coordinator.data.get(DATA_NEARBY, [])
        center = self.coordinator.data.get(DATA_CENTER)
        return {
            "center_latitude": center[0] if center else None,
            "center_longitude": center[1] if center else None,
            "stop_count": len(stops),
            # How old this board is — the thing you want to know before
            # trusting it when updates are slow or on-demand.
            "last_fetched": (
                self.coordinator.last_fetch.isoformat()
                if self.coordinator.last_fetch
                else None
            ),
            # Lets you confirm the quota burn rate at a glance.
            "api_requests_total": self.coordinator.requests_made,
            # Flat, time-sorted board — what a Lovelace card would render.
            "departures": [
                {"stop": stop.name, "distance": stop.distance, **_departure_dict(dep)}
                for stop, dep in self._all_departures
            ],
            # Per-stop breakdown, nearest first.
            "stops": [
                {
                    "id": stop.stop_id,
                    "name": stop.name,
                    "distance": stop.distance,
                    "departures": [
                        _departure_dict(d)
                        for d in stop.departures
                        if _is_upcoming(d)
                    ],
                }
                for stop in stops
            ],
        }


class RejseplanenStopSensor(_BaseSensor):
    """A stable sensor for one explicitly-configured stop."""

    _attr_icon = "mdi:bus-clock"

    def __init__(
        self,
        coordinator: RejseplanenCoordinator,
        entry: RejseplanenConfigEntry,
        stop_id: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._stop_id = stop_id
        self._attr_unique_id = f"{entry.entry_id}_stop_{stop_id}"

    @property
    def _stop(self) -> StopDepartures | None:
        return self.coordinator.data.get(DATA_FIXED, {}).get(self._stop_id)

    @property
    def _upcoming(self) -> list[Departure]:
        stop = self._stop
        if not stop:
            return []
        return [dep for dep in stop.departures if _is_upcoming(dep)]

    @property
    def name(self) -> str:
        stop = self._stop
        return stop.name if stop else self._stop_id

    @property
    def native_value(self) -> datetime | None:
        upcoming = self._upcoming
        return upcoming[0].when if upcoming else None

    @property
    def extra_state_attributes(self) -> dict:
        stop = self._stop
        if not stop:
            return {"stop_id": self._stop_id}
        return {
            "stop_id": stop.stop_id,
            "departures": [_departure_dict(d) for d in self._upcoming],
        }

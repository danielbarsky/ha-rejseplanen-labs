"""Data update coordinator for Rejseplanen Labs.

Quota is the scarce resource here, so the cycle is built to spend as few
requests as possible:

  * nearby stops are cached and only re-looked-up when the centre actually
    moves (see _async_stops),
  * every stop -- nearby and fixed -- is fetched in ONE batched departure
    board rather than one request each,
  * polling can be turned off entirely in favour of on-demand refreshes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    RejseplanenApiError,
    RejseplanenAuthError,
    RejseplanenLabsClient,
    StopDepartures,
)
from .const import (
    CONF_API_KEY,
    CONF_ENABLE_POSITIONS,
    CONF_FIXED_STOPS,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MAX_DEPARTURES,
    CONF_MAX_STOPS,
    CONF_MODE,
    CONF_ON_DEMAND_ONLY,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    CONF_TRACKED_ENTITY,
    DATA_CENTER,
    DATA_FIXED,
    DATA_NEARBY,
    DATA_VEHICLES,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_MAX_STOPS,
    DEFAULT_ON_DEMAND_ONLY,
    DEFAULT_RADIUS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUAL_REFRESH_COOLDOWN,
    MODE_FOLLOW,
    STOP_CACHE_MIN_DISTANCE,
    STOP_CACHE_TTL,
)

_LOGGER = logging.getLogger(__name__)

# Rough metres-per-degree at Copenhagen's latitude (~55.7°N), used to turn the
# search radius into a bounding box for the live-position query.
_M_PER_DEG_LAT = 111_320
_M_PER_DEG_LON = 62_600

_EARTH_RADIUS_M = 6_371_000


class RejseplanenCoordinator(DataUpdateCoordinator[dict]):
    """Resolves the search centre, then fetches nearby + fixed departures."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        opts = {**entry.data, **entry.options}
        self._opts = opts
        self.client = RejseplanenLabsClient(
            async_get_clientsession(hass), opts[CONF_API_KEY]
        )

        # Cached nearby-stop lookup: what we found, where we were standing when
        # we found it, and when.
        self._stops_cache: list[StopDepartures] | None = None
        self._stops_center: tuple[float, float] | None = None
        self._stops_fetched: datetime | None = None
        # Fixed-stop names survive a cycle that returned no departures, so a
        # quiet stop doesn't fall back to displaying its raw id.
        self._fixed_names: dict[str, str] = {}
        # When the data on screen was actually fetched. Plain
        # DataUpdateCoordinator doesn't track this, and with slow or on-demand
        # updates "how old is this board?" is exactly what you want to know.
        self.last_fetch: datetime | None = None

        self.on_demand_only = bool(
            opts.get(CONF_ON_DEMAND_ONLY, DEFAULT_ON_DEMAND_ONLY)
        )
        interval = (
            None
            if self.on_demand_only
            else timedelta(seconds=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=interval,
        )

        # Manual refreshes fire straight away, then collapse for a cooldown, so
        # tapping the button five times while waiting costs one round of calls.
        self._manual_refresh = Debouncer(
            hass,
            _LOGGER,
            cooldown=MANUAL_REFRESH_COOLDOWN,
            immediate=True,
            function=self.async_refresh,
        )

    async def async_manual_refresh(self) -> None:
        """Fetch now, outside the normal schedule (button / service / automation)."""
        _LOGGER.debug("Manual refresh requested for %s", self.entry.title)
        await self._manual_refresh.async_call()

    @property
    def requests_made(self) -> int:
        """Total API requests this instance has made since HA started."""
        return self.client.requests

    def _resolve_center(self) -> tuple[float, float] | None:
        """Return the (lat, lon) to search around, or None if unavailable."""
        if self._opts.get(CONF_MODE) == MODE_FOLLOW:
            entity_id = self._opts.get(CONF_TRACKED_ENTITY)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None:
                return None
            lat = state.attributes.get("latitude")
            lon = state.attributes.get("longitude")
            if lat is None or lon is None:
                return None
            return float(lat), float(lon)
        # Fixed mode.
        lat = self._opts.get(CONF_LATITUDE)
        lon = self._opts.get(CONF_LONGITUDE)
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)

    async def _async_stops(self, center: tuple[float, float]) -> list[StopDepartures]:
        """Nearby stops around `center`, reusing the last lookup where valid.

        Stops don't move, so re-running location.nearbystops every cycle buys
        nothing and costs a request. We re-query only once the centre has
        drifted far enough to plausibly change the answer, or the cache has
        aged out. For a fixed location that means one lookup ever; for a phone
        sitting at home, one lookup until it leaves.

        Returns fresh copies, so the cached metadata is never mutated by the
        departure fetch that follows.
        """
        radius = int(self._opts.get(CONF_RADIUS, DEFAULT_RADIUS))
        max_stops = int(self._opts.get(CONF_MAX_STOPS, DEFAULT_MAX_STOPS))
        # A third of the radius: far enough to ignore GPS jitter, close enough
        # that the stop set can't have meaningfully changed underneath us.
        threshold = max(STOP_CACHE_MIN_DISTANCE, radius // 3)

        if self._stops_cache is not None and self._stops_center is not None:
            moved = _distance_m(self._stops_center, center)
            age = dt_util.utcnow() - self._stops_fetched
            if moved <= threshold and age < STOP_CACHE_TTL:
                _LOGGER.debug(
                    "Reusing %d cached stops (centre moved %dm of %dm allowed, "
                    "cache age %s)",
                    len(self._stops_cache),
                    moved,
                    threshold,
                    age,
                )
                return [replace(stop, departures=[]) for stop in self._stops_cache]

        lat, lon = center
        stops = await self.client.nearby_stops(lat, lon, radius, max_stops)
        self._stops_cache = [replace(stop, departures=[]) for stop in stops]
        self._stops_center = center
        self._stops_fetched = dt_util.utcnow()
        _LOGGER.debug("Looked up %d nearby stops around %s, %s", len(stops), lat, lon)
        return stops

    async def _async_update_data(self) -> dict:
        center = self._resolve_center()
        max_dep = int(self._opts.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES))
        fixed_ids: list[str] = self._opts.get(CONF_FIXED_STOPS, [])

        nearby: list[StopDepartures] = []
        vehicles: list = []

        try:
            if center is not None:
                nearby = await self._async_stops(center)
            elif self._opts.get(CONF_MODE) == MODE_FOLLOW:
                _LOGGER.debug(
                    "Tracked entity %s has no coordinates yet; skipping nearby",
                    self._opts.get(CONF_TRACKED_ENTITY),
                )

            fixed_stops = [
                StopDepartures(
                    stop_id=str(stop_id),
                    name=self._fixed_names.get(str(stop_id), str(stop_id)),
                )
                for stop_id in fixed_ids
            ]

            # One batched board covers nearby AND fixed stops. De-duplicate by
            # id first so a stop that is both doesn't get asked for twice.
            batch: dict[str, StopDepartures] = {}
            for stop in (*nearby, *fixed_stops):
                batch.setdefault(stop.stop_id, stop)
            if batch:
                await self.client.departures_for_stops(list(batch.values()), max_dep)
                # Fan the shared result back out to any duplicate objects.
                for stop in (*nearby, *fixed_stops):
                    source = batch[stop.stop_id]
                    if stop is not source:
                        stop.departures = list(source.departures)

            fixed: dict[str, StopDepartures] = {}
            for stop in fixed_stops:
                # Stop name comes from the per-departure "stop" field, not the
                # line label. Remember it so a quiet cycle doesn't lose it.
                if stop.departures and stop.departures[0].stop_name:
                    stop.name = stop.departures[0].stop_name
                    self._fixed_names[stop.stop_id] = stop.name
                fixed[stop.stop_id] = stop

            if center is not None and self._opts.get(CONF_ENABLE_POSITIONS):
                radius = int(self._opts.get(CONF_RADIUS, DEFAULT_RADIUS))
                bbox = _bounding_box(*center, radius)
                vehicles = await self.client.journey_positions(*bbox)

        except RejseplanenAuthError as err:
            # Re-auth needed — surfaces a repair flow in HA.
            from homeassistant.exceptions import ConfigEntryAuthFailed

            raise ConfigEntryAuthFailed(str(err)) from err
        except RejseplanenApiError as err:
            raise UpdateFailed(str(err)) from err

        self.last_fetch = dt_util.utcnow()
        return {
            DATA_CENTER: center,
            DATA_NEARBY: nearby,
            DATA_FIXED: fixed,
            DATA_VEHICLES: vehicles,
        }


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lon) pairs."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _bounding_box(
    lat: float, lon: float, radius_m: int
) -> tuple[float, float, float, float]:
    """Return (min_lat, min_lon, max_lat, max_lon) around a point."""
    dlat = radius_m / _M_PER_DEG_LAT
    dlon = radius_m / (_M_PER_DEG_LON * max(math.cos(math.radians(lat)), 0.01))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)

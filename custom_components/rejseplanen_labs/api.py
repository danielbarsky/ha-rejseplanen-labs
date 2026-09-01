"""Async client for the Rejseplanen Labs API 2.0.

============================================================================
 ADJUST-WHEN-YOU-HAVE-A-KEY layer
============================================================================
Rejseplanen Labs API 2.0 requires a free API key ("accessId"), obtained by
registering at https://labs.rejseplanen.dk. API 1.0 was shut down on
2024-12-04, so the old key-less xmlopen.rejseplanen.dk endpoints no longer
work.

The endpoint paths, query-parameter names, and JSON response shapes below
follow the HAFAS ReST 2.x conventions Rejseplanen is built on. They are the
*most likely* shapes but are NOT verified against a live key. When you have
one, confirm each request/parser against your Labs docs and tweak only this
file — the rest of the integration consumes the dataclasses below and does
not care about the wire format.

Confirm in particular:
  * BASE_URL and the three service paths.
  * Coordinate encoding (decimal degrees vs. 1e6 integers). API 2.0 ReST
    uses decimal degrees; API 1.0 used lat*1_000_000. We use decimal here.
  * The response envelope keys (`stopLocationOrCoordLocation`, `Departure`,
    …) — HAFAS nests these inconsistently, hence the defensive parsing.
  * The live-position service: `journey_positions()` is a documented STUB.
    The Labs "journey position" service returns real-time vehicle positions
    within a map region, but its exact contract is not public without a key.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Rejseplanen returns local Danish wall-clock times (confirmed: timezoneOffset
# 120 / planRtTs +02:00). Attach this zone so datetimes are tz-aware — HA
# timestamp sensors require it — and DST is handled per-date.
_TZ = ZoneInfo("Europe/Copenhagen")

# -- Endpoints (CONFIRM against your Labs docs) ------------------------------
BASE_URL = "https://www.rejseplanen.dk/api"
PATH_NEARBY_STOPS = "location.nearbystops"
PATH_DEPARTURE_BOARD = "departureBoard"
PATH_MULTI_DEPARTURE_BOARD = "multiDepartureBoard"
# The live-position service path is a guess; see journey_positions().
PATH_JOURNEY_POS = "journeyPos"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Cap concurrent departure-board calls when fanning out over nearby stops so
# we stay friendly to the Labs quota.
_MAX_CONCURRENCY = 5
# A stop whose individual board came back genuinely empty isn't re-queried
# for this long. Bounds the cost of quiet stops at 03:00 without ever leaving
# one permanently un-fetched.
_EMPTY_RECHECK_AFTER = timedelta(minutes=10)
# If multiDepartureBoard errors out, stop trying for this long. An unsupported
# endpoint then costs one wasted request an hour instead of one per cycle.
_MULTI_RETRY_AFTER = timedelta(hours=1)


class RejseplanenApiError(Exception):
    """Raised for any API-level failure (network, auth, bad payload)."""


class RejseplanenAuthError(RejseplanenApiError):
    """Raised when the API key is missing/invalid (HTTP 401/403)."""


@dataclass
class Departure:
    """A single upcoming departure at a stop."""

    name: str  # display label, e.g. "Bus 5C" / "Metro M3"
    type: str  # transport category from ProductAtStop.catOut: "Bus", "MET", …
    line: str  # "5C", "M3", "250S"
    operator: str  # "Movia", "Metroselskabet"
    direction: str  # destination / headsign
    scheduled: datetime | None
    realtime: datetime | None  # None when no live data
    stop_name: str = ""  # per-departure "stop" field
    stop_ext_id: str = ""  # per-departure "stopExtId" (which quay it leaves from)
    track: str | None = None
    cancelled: bool = False
    journey_id: str | None = None  # ref for journey detail / position

    @property
    def when(self) -> datetime | None:
        """Best available time: realtime if present, else scheduled."""
        return self.realtime or self.scheduled

    @property
    def is_realtime(self) -> bool:
        return self.realtime is not None


@dataclass
class StopDepartures:
    """A stop plus its upcoming departures."""

    stop_id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    distance: int | None = None  # metres from search centre
    departures: list[Departure] = field(default_factory=list)


@dataclass
class VehiclePosition:
    """A live vehicle position (Buster-style live map data)."""

    journey_id: str
    name: str
    type: str
    latitude: float
    longitude: float
    direction: str | None = None


class RejseplanenLabsClient:
    """Thin wrapper over the Labs REST API. Isolates all wire-format details."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        self._session = session
        self._api_key = api_key
        # Running total of requests this client has made, so the burn rate is
        # observable rather than something you discover when the quota dies.
        self.requests = 0
        self._multi_retry_after: datetime | None = None
        # stop_id -> don't bother re-fetching before this time (verified empty)
        self._empty_until: dict[str, datetime] = {}
        # stopExtIds the last batched board returned that matched no requested
        # stop. Purely diagnostic -- this is what you need to fix attribution.
        self._unattributed: list[str] = []

    async def _get(self, path: str, params: dict) -> dict:
        """Perform a GET, returning parsed JSON. Raises RejseplanenApiError."""
        query = {**params, "accessId": self._api_key, "format": "json"}
        url = f"{BASE_URL}/{path}"
        self.requests += 1
        try:
            async with self._session.get(
                url, params=query, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise RejseplanenAuthError(
                        f"Auth failed ({resp.status}) — check API key"
                    )
                if resp.status >= 400:
                    body = await resp.text()
                    raise RejseplanenApiError(f"HTTP {resp.status}: {body[:200]}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise RejseplanenApiError(f"Request to {path} failed: {err}") from err

    # -- Public API ---------------------------------------------------------

    async def nearby_stops(
        self, latitude: float, longitude: float, radius_m: int, max_no: int
    ) -> list[StopDepartures]:
        """Return stops within `radius_m` of the coordinate, nearest first."""
        # The API is strict: integer params must be ints, not "4.0". HA's
        # NumberSelector hands us floats, so coerce at the boundary.
        payload = await self._get(
            PATH_NEARBY_STOPS,
            {
                "originCoordLat": latitude,
                "originCoordLong": longitude,
                "r": int(radius_m),
                "maxNo": int(max_no),
            },
        )
        return _parse_nearby_stops(payload)

    async def departure_board(
        self, stop_id: str, max_departures: int
    ) -> list[Departure]:
        """Return upcoming departures that actually leave from `stop_id`.

        Rejseplanen's board is *station*-level: a single id returns departures
        for every quay/platform of the station, each tagged with its own
        `stopExtId`. We therefore request extra, then keep only the departures
        whose stopExtId matches the id we asked for, so sibling quays don't
        bleed in. If none match (e.g. a station-level id, or untagged data) we
        fall back to the full board rather than showing nothing.
        """
        stop_id = str(stop_id)
        # Ask for headroom so that, after filtering to one quay, we still have
        # enough to fill max_departures.
        request_count = min(max(int(max_departures) * 5, 30), 60)
        payload = await self._get(
            PATH_DEPARTURE_BOARD,
            {"id": stop_id, "maxJourneys": request_count},
        )
        departures = _parse_departures(payload)
        own = [d for d in departures if d.stop_ext_id == stop_id]
        result = own if own else departures
        return result[: int(max_departures)]

    async def multi_departure_board(
        self, stop_ids: list[str], max_departures: int
    ) -> dict[str, list[Departure]]:
        """Fetch boards for many stops in ONE request, keyed by requested id.

        HAFAS's multiDepartureBoard takes an id list and returns a single flat
        Departure list, each entry tagged with the stopExtId it leaves from --
        which is how we regroup it. Stops the board said nothing about are
        simply absent from the result; the caller decides what to do about it.

        NOT VERIFIED against a live key: the `idList` separator and the
        response envelope follow the HAFAS 2.x convention, but Rejseplanen may
        differ. A wrong guess is safe -- departures_for_stops catches the error,
        disables batching for an hour, and falls back to per-stop boards.
        """
        ids = [str(i) for i in stop_ids]
        if not ids:
            return {}
        # maxJourneys caps the WHOLE board here, not each stop, so scale it by
        # the number of stops and leave headroom for sibling quays.
        per_stop = max(int(max_departures) * 3, 10)
        payload = await self._get(
            PATH_MULTI_DEPARTURE_BOARD,
            {
                "idList": ";".join(ids),
                "maxJourneys": min(per_stop * len(ids), 250),
            },
        )

        grouped: dict[str, list[Departure]] = {}
        unattributed: list[str] = []
        for dep in _parse_departures(payload):
            owner = _attribute_stop(dep.stop_ext_id, ids)
            if owner is not None:
                grouped.setdefault(owner, []).append(dep)
            elif dep.stop_ext_id not in unattributed:
                unattributed.append(dep.stop_ext_id)
        self._unattributed = unattributed
        if unattributed:
            _LOGGER.debug(
                "multiDepartureBoard returned departures at stopExtIds %s that "
                "match none of the requested ids %s",
                unattributed,
                ids,
            )

        return {
            stop_id: _sorted_departures(deps)[: int(max_departures)]
            for stop_id, deps in grouped.items()
        }

    async def departures_for_stops(
        self, stops: list[StopDepartures], max_departures: int
    ) -> list[StopDepartures]:
        """Fill in departures for many stops in as few requests as possible.

        A single multiDepartureBoard call covers every stop, which is the
        biggest quota saving available here: the previous per-stop fan-out cost
        one request per stop per cycle. Any stop the batch didn't answer for
        falls back to an individual board.

        Stops are mutated in place (and also returned) as before.
        """
        if not stops:
            return []

        grouped: dict[str, list[Departure]] = {}
        if self._multi_available():
            try:
                grouped = await self.multi_departure_board(
                    [stop.stop_id for stop in stops], max_departures
                )
            except RejseplanenAuthError:
                raise  # a bad key is not something falling back can fix
            except RejseplanenApiError as err:
                self._disable_multi()
                _LOGGER.warning(
                    "multiDepartureBoard failed (%s) -- falling back to per-stop "
                    "boards; will retry the batched call after %s",
                    err,
                    self._multi_retry_after,
                )

        for stop in stops:
            stop.departures = grouped.get(stop.stop_id, [])

        missing = [stop for stop in stops if not stop.departures]
        if not missing:
            return stops

        if not grouped:
            # The batch accounted for nothing at all. Either every stop really
            # is quiet, or attribution is broken -- fetch them all and let the
            # answer decide. Never truncate this list: doing so starves the
            # same trailing stops on every single cycle.
            await self._fill_individually(missing, max_departures)
            if any(stop.departures for stop in missing):
                # Individual boards found departures the batch failed to
                # attribute to any stop. That's the batched board not working.
                self._disable_multi()
                _LOGGER.warning(
                    "multiDepartureBoard returned nothing attributable for %d "
                    "stops but individual boards found departures -- stop id "
                    "attribution is wrong. Requested ids %s; board reported "
                    "departures at stopExtIds %s. Falling back to per-stop "
                    "boards, retrying the batched call after %s",
                    len(missing),
                    [stop.stop_id for stop in missing],
                    self._unattributed or "(none -- board was empty)",
                    self._multi_retry_after,
                )
            return stops

        # The batch worked for some stops, so the rest are plausibly just
        # quiet. Confirm individually, then leave verified-empty stops alone
        # for a while rather than re-asking every cycle.
        now = datetime.now(_TZ)
        due = [
            stop
            for stop in missing
            if self._empty_until.get(stop.stop_id, now) <= now
        ]
        skipped = len(missing) - len(due)
        if skipped:
            _LOGGER.debug(
                "Skipping %d stop(s) confirmed empty within the last %s",
                skipped,
                _EMPTY_RECHECK_AFTER,
            )
        if due:
            await self._fill_individually(due, max_departures)
            for stop in due:
                if stop.departures:
                    self._empty_until.pop(stop.stop_id, None)
                else:
                    self._empty_until[stop.stop_id] = now + _EMPTY_RECHECK_AFTER

        return stops

    async def _fill_individually(
        self, stops: list[StopDepartures], max_departures: int
    ) -> None:
        """Per-stop boards, concurrency-capped. One bad stop doesn't sink the rest."""
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def _fill(stop: StopDepartures) -> None:
            async with semaphore:
                try:
                    stop.departures = await self.departure_board(
                        stop.stop_id, max_departures
                    )
                except RejseplanenApiError as err:
                    _LOGGER.debug("Departure board failed for %s: %s", stop.name, err)

        await asyncio.gather(*(_fill(stop) for stop in stops))

    def _multi_available(self) -> bool:
        """Whether to attempt the batched board this cycle."""
        return (
            self._multi_retry_after is None
            or datetime.now(_TZ) >= self._multi_retry_after
        )

    def _disable_multi(self) -> None:
        self._multi_retry_after = datetime.now(_TZ) + _MULTI_RETRY_AFTER

    async def journey_positions(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
    ) -> list[VehiclePosition]:
        """Return live vehicle positions within a bounding box.

        STUB — the Labs "journey position" service exists (it powers the
        Buster-style live map) but its exact request/response is not public
        without a key. Wire it up here once confirmed; until then this returns
        [] so the device_tracker platform loads but stays inert rather than
        crashing the whole integration.
        """
        _LOGGER.debug(
            "journey_positions() is a stub — implement against Labs docs "
            "(bbox %s,%s -> %s,%s)",
            min_lat,
            min_lon,
            max_lat,
            max_lon,
        )
        return []


# -- Defensive parsers (HAFAS nests things inconsistently) ------------------


def _as_list(value) -> list:
    """HAFAS returns a dict for one item and a list for many. Normalise."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _parse_time(date_str: str | None, time_str: str | None) -> datetime | None:
    if not date_str or not time_str:
        return None
    try:
        # HAFAS times can be "25:03:00" (next day). Fold hours >= 24.
        hh, mm, ss = (time_str.split(":") + ["0", "0"])[:3]
        extra_days, hour = divmod(int(hh), 24)
        base = datetime.strptime(
            f"{date_str} {hour:02d}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=_TZ)
        if extra_days:
            base = base + timedelta(days=extra_days)
        return base
    except (ValueError, TypeError):
        return None


def _sorted_departures(departures: list[Departure]) -> list[Departure]:
    """Chronological, with time-less entries last."""
    far_future = datetime.max.replace(tzinfo=_TZ)
    return sorted(
        departures,
        key=lambda dep: (dep.when is None, dep.when or far_future),
    )


def _normalise_id(stop_id: str) -> str:
    """Danish stop ids appear both zero-padded and bare; compare the bare form."""
    return str(stop_id).lstrip("0")


def _attribute_stop(stop_ext_id: str, requested: list[str]) -> str | None:
    """Map a departure's stopExtId back to one of the ids we asked for.

    An exact match wins. Otherwise we lean on the fact that quay ids extend
    their station id (station 8600626 -> quay 86006261) and match on the
    longest shared prefix in either direction, so a station-level request still
    collects its platforms and a quay-level request still finds its station's
    board.

    This is a heuristic over an unverified response shape. If boards come back
    empty, turn on debug logging and check this before anything else.
    """
    if not stop_ext_id:
        return None
    if stop_ext_id in requested:
        return stop_ext_id

    target = _normalise_id(stop_ext_id)
    best: str | None = None
    for candidate in requested:
        bare = _normalise_id(candidate)
        if not bare:
            continue
        if target.startswith(bare) or bare.startswith(target):
            # Prefer the most specific id that still matches.
            if best is None or len(_normalise_id(best)) < len(bare):
                best = candidate
    return best


def _parse_nearby_stops(payload: dict) -> list[StopDepartures]:
    container = payload.get("stopLocationOrCoordLocation") or payload.get(
        "StopLocation"
    )
    stops: list[StopDepartures] = []
    for entry in _as_list(container):
        # Entries may be wrapped as {"StopLocation": {...}} or bare.
        node = entry.get("StopLocation", entry) if isinstance(entry, dict) else {}
        stop_id = node.get("extId") or node.get("id")
        if not stop_id:
            continue
        stops.append(
            StopDepartures(
                stop_id=str(stop_id),
                name=node.get("name", "Unknown stop"),
                latitude=_to_float(node.get("lat")),
                longitude=_to_float(node.get("lon")),
                distance=_to_int(node.get("dist")),
            )
        )
    return stops


def _parse_departures(payload: dict) -> list[Departure]:
    departures: list[Departure] = []
    for node in _as_list(payload.get("Departure")):
        if not isinstance(node, dict):
            continue
        # Mode / line / operator live in ProductAtStop; the top-level "type"
        # is always "ST" (a stop marker) and must NOT be used as the category.
        product = node.get("ProductAtStop") or {}
        departures.append(
            Departure(
                name=(node.get("name") or product.get("name") or "").strip(),
                type=(product.get("catOut") or product.get("catOutL") or "").strip(),
                line=(product.get("line") or product.get("displayNumber") or "").strip(),
                operator=(product.get("operator") or "").strip(),
                direction=node.get("direction") or node.get("directionFlag", ""),
                scheduled=_parse_time(node.get("date"), node.get("time")),
                realtime=_parse_time(node.get("rtDate"), node.get("rtTime")),
                stop_name=node.get("stop", ""),
                stop_ext_id=str(node.get("stopExtId") or ""),
                track=node.get("rtTrack") or node.get("track") or _platform(node),
                cancelled=bool(node.get("cancelled", False)),
                journey_id=_journey_ref(node),
            )
        )
    return departures


def _platform(node: dict) -> str | None:
    platform = node.get("platform")
    if isinstance(platform, dict):
        return platform.get("text")
    return None


def _journey_ref(node: dict) -> str | None:
    ref = node.get("JourneyDetailRef")
    if isinstance(ref, dict):
        return ref.get("ref")
    return node.get("journeyid") or None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

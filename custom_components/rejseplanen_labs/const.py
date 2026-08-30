"""Constants for the Rejseplanen Labs integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "rejseplanen_labs"

# --- Config entry / options keys -------------------------------------------
CONF_API_KEY = "api_key"

# How the search centre is determined.
CONF_MODE = "mode"
MODE_FOLLOW = "follow"  # follow a person / device_tracker entity
MODE_FIXED = "fixed"  # a static coordinate

CONF_TRACKED_ENTITY = "tracked_entity"  # entity_id when mode == follow
CONF_LATITUDE = "latitude"  # when mode == fixed
CONF_LONGITUDE = "longitude"  # when mode == fixed

CONF_RADIUS = "radius"  # metres
CONF_MAX_STOPS = "max_stops"  # cap nearby stops we fan out to
CONF_MAX_DEPARTURES = "max_departures"  # per stop
CONF_FIXED_STOPS = "fixed_stops"  # explicit stop ids (stable entities)
CONF_ENABLE_POSITIONS = "enable_positions"  # live vehicle positions
CONF_SCAN_INTERVAL = "scan_interval"  # seconds
CONF_ON_DEMAND_ONLY = "on_demand_only"  # never poll on a timer

# --- Defaults ---------------------------------------------------------------
DEFAULT_RADIUS = 700  # metres
DEFAULT_MAX_STOPS = 8
DEFAULT_MAX_DEPARTURES = 6
DEFAULT_SCAN_INTERVAL = 60  # seconds
DEFAULT_ON_DEMAND_ONLY = False
MIN_SCAN_INTERVAL = timedelta(seconds=30)

# --- Quota management -------------------------------------------------------
# Stops don't move, so location.nearbystops is only re-run once the search
# centre has drifted this far. Scaled off the radius (a third of it), floored
# here so a tiny radius doesn't re-query on GPS jitter alone.
STOP_CACHE_MIN_DISTANCE = 100  # metres
# ...and re-run anyway this often, so timetable changes eventually land.
STOP_CACHE_TTL = timedelta(hours=6)

# Entities re-render this often to keep countdowns honest and drop services
# that already left. Costs no API calls — it only re-reads cached data.
LOCAL_TICK = timedelta(seconds=30)
# A departure stays on the board this long after its time, so a bus you can
# still see doesn't vanish a second past due.
DEPARTED_GRACE = timedelta(minutes=1)

# Manual refreshes fire immediately, then are swallowed for this long, so a
# button tapped repeatedly at the bus stop costs one round of calls.
MANUAL_REFRESH_COOLDOWN = 15  # seconds

# --- Coordinator data keys --------------------------------------------------
DATA_NEARBY = "nearby"  # list[StopDepartures] ordered by distance
DATA_FIXED = "fixed"  # dict[stop_id, StopDepartures]
DATA_VEHICLES = "vehicles"  # list[VehiclePosition]
DATA_CENTER = "center"  # (lat, lon) actually used this cycle

# --- Services ---------------------------------------------------------------
SERVICE_REFRESH = "refresh"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

ATTR_ATTRIBUTION = "Data from Rejseplanen Labs (CC BY 4.0)"

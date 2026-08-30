# Rejseplanen Labs for Home Assistant

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]
[![license][license-badge]](LICENSE)

Real-time Danish public-transport departures — bus, metro, S-train, regional/IC,
including Movia buses — for Home Assistant, using the **Rejseplanen Labs API
2.0**.

Shows upcoming departures for **all stops within a radius** of a moving or fixed
location, plus **stable per-stop sensors**, and (once wired) live vehicle
positions as device trackers.

> The built-in `rejseplanen` integration is broken because API 1.0 (the key-less
> `xmlopen.rejseplanen.dk` endpoints) shut down on 2024-12-04. This uses API 2.0,
> which needs a free API key.

## Status

- **Departures: verified.** `location.nearbystops` and `departureBoard` were
  confirmed against a live key (server 2.53-Rejseplanen); the parsers match the
  real response shapes — mode/line/operator from `ProductAtStop`, tz-aware
  Europe/Copenhagen times, real-time `rtTime`/`rtDate`.
- **Batched boards: unverified.** `multiDepartureBoard` follows the HAFAS 2.x
  convention but hasn't been confirmed against a live key. It fails safe — see
  [Notes](#notes).
- **Live positions: not wired.** `journey_positions()` is a documented stub
  returning `[]`, so the `device_tracker` platform loads but creates no
  entities. Everything else works without it.

## Installation

### HACS (custom repository)

1. In Home Assistant, go to **HACS → ⋮ (top right) → Custom repositories**.
2. Add `https://github.com/danielbarsky/ha-rejseplanen-labs` with category
   **Integration**.
3. Find **Rejseplanen Labs** in HACS, click **Download**.
4. Restart Home Assistant.

### Manual

Copy `custom_components/rejseplanen_labs/` into your `config/custom_components/`
directory and restart Home Assistant.

## Setup

1. Get a free API key (an "accessId") at <https://labs.rejseplanen.dk>.
2. **Settings → Devices & Services → Add Integration → "Rejseplanen Labs"**.
3. Enter your API key and pick a mode:
   - **Follow a person / tracker** — centres on that entity's GPS and re-centres
     as it moves (e.g. your phone via the Companion app).
   - **Fixed location** — a static coordinate, e.g. home.
4. Set radius, max stops, departures per stop, optional fixed stop IDs, and
   whether to enable live positions.
5. Pick an update interval — or tick **Only update on demand** and drive it
   entirely from the refresh button. See
   [Managing the API quota](#managing-the-api-quota).

Add the integration **multiple times** for multiple instances — e.g. one
following your phone and one fixed at home.

## Entities

| Entity | What it is |
|---|---|
| `sensor.<title>_nearby` | Aggregate board. State is the next departure anywhere in range; `attributes.departures` is the flat time-sorted list, `attributes.stops` the per-stop breakdown (nearest first). |
| `sensor.<stop name>` | One per configured fixed stop. |
| `button.<title>_refresh_departures` | Fetch now, ignoring the interval. |
| `device_tracker.*` | One per live vehicle (once `journey_positions()` is implemented). |

## Finding fixed stop IDs

Use `location.nearbystops` (or the Labs stop search) to get `extId`/`id` values
for the stops you care about, and paste them into the **Fixed Stop IDs** field.

## Managing the API quota

The Labs quota is the real constraint, especially with one instance per person.
Three things keep the burn rate down:

**One batched request per cycle.** Every stop — nearby *and* fixed — is fetched
in a single `multiDepartureBoard` call instead of one `departureBoard` per stop.
That alone is most of the saving: a default 8-stop instance went from ~9 requests
per cycle to 1.

**Cached stop lookups.** `location.nearbystops` only re-runs once the search
centre has drifted more than a third of the radius (minimum 100 m), or the cache
is 6 hours old. A fixed-location instance does this lookup once; a phone sitting
at home does it once until it leaves.

**Local re-rendering.** Countdowns are recomputed from cached timestamps every
30 s, and departed services drop off the board, without any API traffic. This is
what makes a slow or on-demand interval usable — only real-time delay revisions
need an actual fetch, so a 5-minute interval still shows an accurate
"leaves in 3 min".

Roughly, per instance per day:

| Setup | Requests/day |
|---|---|
| 60 s interval, 8 stops (naive per-stop fan-out) | ~13,000 |
| 60 s interval, batched + cached stops | ~1,450 |
| 5 min interval, batched + cached stops | ~290 |
| On-demand only | one per press |

Watch `sensor.<title>_nearby` → `api_requests_total` for the real number, and
`last_fetched` for how old the board is.

### Refreshing on demand

Any of these fetch immediately, whatever the interval:

- press `button.<title>_refresh_departures`, or wire it to a dashboard
  `tap_action`
- call the `rejseplanen_labs.refresh` service, optionally targeting one instance
- call `homeassistant.update_entity` on one of the sensors

Presses within 15 seconds collapse into a single fetch, so tapping impatiently at
the bus stop costs one round of calls. Useful triggers: a wall display waking, a
hallway motion sensor, or leaving a zone.

```yaml
automation:
  - alias: Fresh departures when the hallway display wakes
    triggers:
      - trigger: state
        entity_id: binary_sensor.hallway_display_active
        to: "on"
    actions:
      - action: rejseplanen_labs.refresh
```

## Notes

- `multiDepartureBoard`'s request and response shape is the HAFAS 2.x convention
  but is **not yet verified** against a live key. If it returns an error, the
  client logs a warning, falls back to per-stop boards, and retries the batched
  call an hour later — so a wrong guess costs quota, not correctness. If boards
  come back empty, enable debug logging and check `_attribute_stop()` in
  `api.py` first; it maps a departure's `stopExtId` back to the stop you asked
  for via shared-prefix matching.

  ```yaml
  logger:
    logs:
      custom_components.rejseplanen_labs: debug
  ```

- Departure times are absolute timestamps in `attributes.departures`
  (`scheduled` / `realtime`), so a Lovelace card can compute its own countdown
  and stay accurate between fetches.

## Credits

Transit data © [Rejseplanen Labs](https://labs.rejseplanen.dk), licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This project is not
affiliated with or endorsed by Rejseplanen.

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/danielbarsky/ha-rejseplanen-labs
[release-url]: https://github.com/danielbarsky/ha-rejseplanen-labs/releases
[license-badge]: https://img.shields.io/github/license/danielbarsky/ha-rejseplanen-labs

"""Tests for stop-id attribution and batched-board request shaping.

Runs without Home Assistant: api.py only needs aiohttp.
"""
import asyncio, importlib.util, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "rejseplanen_labs"
spec = importlib.util.spec_from_file_location("rp_api", ROOT / "api.py")
api = importlib.util.module_from_spec(spec); sys.modules["rp_api"] = api; spec.loader.exec_module(api)

fails = []
def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok: fails.append(label)

A = api._attribute_stop
check("exact match", A("8600626", ["8600626", "8600020"]), "8600626")
check("quay id extends station id", A("86006261", ["8600626"]), "8600626")
check("station id when a quay was requested", A("8600626", ["86006261"]), "86006261")
check("zero-padded ids normalise", A("008600626", ["8600626"]), "8600626")
check("prefers the most specific match", A("860062612", ["8600626", "86006261"]), "86006261")
check("unrelated id attributes to nothing", A("9999999", ["8600626"]), None)
check("empty stopExtId attributes to nothing", A("", ["8600626"]), None)

TZ = api._TZ
def d(minute, name):
    t = datetime(2026, 9, 1, 8, minute, tzinfo=TZ) if minute is not None else None
    return api.Departure(name=name, type="Bus", line="X", operator="Movia",
                         direction="Y", scheduled=t, realtime=None)
check("sorted chronologically, timeless last",
      [x.name for x in api._sorted_departures([d(30,"c"), d(None,"none"), d(10,"a"), d(20,"b")])],
      ["a","b","c","none"])

PAYLOAD = {"Departure": [
    {"name":"Bus 5C","stop":"A","stopExtId":"86006261","date":"2026-09-01","time":"08:10:00",
     "direction":"Herlev","ProductAtStop":{"catOut":"Bus","line":"5C","operator":"Movia"}},
    {"name":"Bus 350S","stop":"A","stopExtId":"86006261","date":"2026-09-01","time":"08:05:00",
     "direction":"Ballerup","ProductAtStop":{"catOut":"Bus","line":"350S","operator":"Movia"}},
    {"name":"Metro M3","stop":"B","stopExtId":"86000201","date":"2026-09-01","time":"08:02:00",
     "direction":"Ring","ProductAtStop":{"catOut":"MET","line":"M3","operator":"Metro"}},
]}

class FakeClient(api.RejseplanenLabsClient):
    def __init__(self, payloads):
        super().__init__(session=None, api_key="x")
        self.payloads, self.calls = payloads, []
    async def _get(self, path, params):
        self.requests += 1
        self.calls.append((path, params))
        r = self.payloads.get(path)
        if isinstance(r, Exception): raise r
        return r

async def main():
    c = FakeClient({api.PATH_MULTI_DEPARTURE_BOARD: PAYLOAD,
                    api.PATH_DEPARTURE_BOARD: {"Departure": []}})
    s = [api.StopDepartures(stop_id=i, name=i) for i in ["8600626","8600020"]]
    await c.departures_for_stops(s, 6)
    check("batch groups by station prefix, time-sorted",
          [x.line for x in s[0].departures], ["350S","5C"])
    check("second stop grouped", [x.line for x in s[1].departures], ["M3"])
    check("idList uses semicolons", c.calls[0][1]["idList"], "8600626;8600020")

    # A bad key must not be masked by the per-stop fallback.
    c2 = FakeClient({api.PATH_MULTI_DEPARTURE_BOARD: api.RejseplanenAuthError("401")})
    try:
        await c2.departures_for_stops([api.StopDepartures(stop_id="1", name="1")], 6)
        check("auth error propagates", "no raise", "RejseplanenAuthError")
    except api.RejseplanenAuthError:
        check("auth error propagates", True, True)

    # After a batch error the endpoint isn't retried on the next cycle.
    c3 = FakeClient({api.PATH_MULTI_DEPARTURE_BOARD: api.RejseplanenApiError("HTTP 500"),
                     api.PATH_DEPARTURE_BOARD: {"Departure": []}})
    ids = ["1","2"]
    await c3.departures_for_stops([api.StopDepartures(stop_id=i, name=i) for i in ids], 6)
    n = sum(1 for p,_ in c3.calls if p == api.PATH_MULTI_DEPARTURE_BOARD)
    await c3.departures_for_stops([api.StopDepartures(stop_id=i, name=i) for i in ids], 6)
    check("batched endpoint not retried within the backoff",
          sum(1 for p,_ in c3.calls if p == api.PATH_MULTI_DEPARTURE_BOARD), n)

    # Unattributed stopExtIds are recorded for diagnostics.
    c4 = FakeClient({api.PATH_MULTI_DEPARTURE_BOARD:
                        {"Departure": [{"name":"X","stop":"Z","stopExtId":"77777777",
                                        "date":"2026-09-01","time":"08:00:00","direction":"Q",
                                        "ProductAtStop":{"catOut":"Bus","line":"1A"}}]},
                     api.PATH_DEPARTURE_BOARD: {"Departure": []}})
    await c4.departures_for_stops([api.StopDepartures(stop_id="8600626", name="A")], 6)
    check("unattributed stopExtIds recorded for diagnostics", c4._unattributed, ["77777777"])

asyncio.run(main())
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

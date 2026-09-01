"""Regression tests for the 4-stops-only-3-refresh bug."""
import asyncio, importlib.util, sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "rejseplanen_labs"
spec = importlib.util.spec_from_file_location("rp_api", ROOT / "api.py")
api = importlib.util.module_from_spec(spec); sys.modules["rp_api"] = api; spec.loader.exec_module(api)

fails = []
def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"\n      got={got!r}\n     want={want!r}"))
    if not ok: fails.append(label)

TZ = api._TZ
def dep(stop_ext_id, line, minute=10):
    return {"name": f"Bus {line}", "stop": f"Stop {stop_ext_id}", "stopExtId": stop_ext_id,
            "date": "2026-09-01", "time": f"08:{minute:02d}:00", "direction": "X",
            "ProductAtStop": {"catOut": "Bus", "line": line, "operator": "Movia"}}

class FakeClient(api.RejseplanenLabsClient):
    def __init__(self, multi_payload, board_payloads=None, board_default=None):
        super().__init__(session=None, api_key="x")
        self.multi_payload = multi_payload
        self.board_payloads = board_payloads or {}
        self.board_default = board_default if board_default is not None else {"Departure": []}
        self.board_calls = []
    async def _get(self, path, params):
        self.requests += 1
        if path == api.PATH_MULTI_DEPARTURE_BOARD:
            if isinstance(self.multi_payload, Exception): raise self.multi_payload
            return self.multi_payload
        self.board_calls.append(params["id"])
        return self.board_payloads.get(params["id"], self.board_default)

def stops(*ids): return [api.StopDepartures(stop_id=i, name=f"Stop {i}") for i in ids]

async def main():
    IDS = ["100", "200", "300", "400"]

    # --- THE REPORTED BUG -------------------------------------------------
    # 4 nearby stops; the batched board attributes nothing (unverified
    # endpoint). Every stop must still get an individual board.
    c = FakeClient(multi_payload={"Departure": []},
                   board_payloads={i: {"Departure": [dep(i, f"L{i}")]} for i in IDS})
    s = stops(*IDS)
    await c.departures_for_stops(s, 6)
    check("all 4 stops fetched individually", sorted(c.board_calls), sorted(IDS))
    check("all 4 stops have departures", [bool(x.departures) for x in s], [True]*4)
    check("4th stop specifically populated", [d.line for d in s[3].departures], ["L400"])

    # Attribution proven broken -> batching disabled for the retry window.
    check("batching disabled after proven bad attribution", c._multi_available(), False)

    # --- No starvation across repeated cycles -----------------------------
    c2 = FakeClient(multi_payload={"Departure": []},
                    board_payloads={i: {"Departure": [dep(i, f"L{i}")]} for i in IDS})
    seen = set()
    for _ in range(3):
        s2 = stops(*IDS)
        await c2.departures_for_stops(s2, 6)
        seen |= {x.stop_id for x in s2 if x.departures}
    check("every stop covered across cycles (no permanent hole)", sorted(seen), sorted(IDS))

    # --- Genuinely quiet stop: fetched, then backed off, never starved -----
    # Batch works for 3; stop 400 is really empty.
    multi = {"Departure": [dep("100","5C"), dep("200","350S"), dep("300","M3")]}
    c3 = FakeClient(multi_payload=multi, board_default={"Departure": []})
    s3 = stops(*IDS)
    await c3.departures_for_stops(s3, 6)
    check("partial batch: quiet stop still probed individually", c3.board_calls, ["400"])
    check("partial batch: batching stays enabled", c3._multi_available(), True)
    check("partial batch: other 3 populated from batch", [bool(x.departures) for x in s3[:3]], [True]*3)

    before = len(c3.board_calls)
    s3b = stops(*IDS)
    await c3.departures_for_stops(s3b, 6)
    check("verified-empty stop not re-probed next cycle", len(c3.board_calls), before)

    # ...but it IS re-probed once the backoff expires.
    c3._empty_until["400"] = datetime.now(TZ) - timedelta(seconds=1)
    s3c = stops(*IDS)
    await c3.departures_for_stops(s3c, 6)
    check("re-probed after backoff expires", len(c3.board_calls), before + 1)

    # A stop that starts producing again clears its backoff.
    c3.board_payloads["400"] = {"Departure": [dep("400","1A")]}
    c3._empty_until["400"] = datetime.now(TZ) - timedelta(seconds=1)
    s3d = stops(*IDS)
    await c3.departures_for_stops(s3d, 6)
    check("stop with departures again is populated", [d.line for d in s3d[3].departures], ["1A"])
    check("backoff cleared once it has departures", "400" in c3._empty_until, False)

    # --- All stops genuinely quiet: batching must NOT be disabled ---------
    c4 = FakeClient(multi_payload={"Departure": []}, board_default={"Departure": []})
    await c4.departures_for_stops(stops(*IDS), 6)
    check("all-quiet night does not disable batching", c4._multi_available(), True)

    # --- Batch endpoint erroring still falls back for every stop ----------
    c5 = FakeClient(multi_payload=api.RejseplanenApiError("HTTP 500"),
                    board_payloads={i: {"Departure": [dep(i, f"L{i}")]} for i in IDS})
    s5 = stops(*IDS)
    await c5.departures_for_stops(s5, 6)
    check("HTTP 500 on batch -> all 4 still fetched", sorted(c5.board_calls), sorted(IDS))

asyncio.run(main())
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

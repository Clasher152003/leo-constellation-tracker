"""
Multi-Satellite Constellation Dashboard - Backend
FastAPI + Skyfield. Tracks multiple satellites with per-satellite 10-minute
TLE caching backed by CelesTrak. If CelesTrak fails for a satellite, its
hardcoded fallback TLE (if provided) is used. If no fallback is provided
and CelesTrak fails, that satellite is skipped from the response rather
than fabricating data.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from skyfield.api import EarthSatellite, load

logger = logging.getLogger("constellation")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Config: tracked satellites
# ---------------------------------------------------------------------------

CELESTRAK_TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=TLE"
TLE_CACHE_SECONDS = 10 * 60  # 10 minutes

# Registry of tracked satellites: norad_id -> display metadata.
# fallback_line1 / fallback_line2 are used ONLY if CelesTrak fails AND
# no cached TLE exists yet. Leave them as None if you don't have a
# verified real TLE on hand — the satellite will simply be omitted from
# the response rather than showing fabricated data.
#
# NOTE: ISS fallback below was pulled from CelesTrak (epoch 2025-day308).
# It WILL drift out of date. For Hubble / NOAA-20 / Starlink-1007, no
# verified fallback is included yet — paste in a real TLE from
# https://celestrak.org/NORAD/elements/gp.php?CATNR=<id>&FORMAT=TLE
# if you want those to survive a CelesTrak outage.
TRACKED_SATELLITES = {
    "25544": {
        "name": "ISS (ZARYA)",
        "color": "#4be3ff",
        "fallback_line1": "1 25544U 98067A   25308.35786713  .00010709  00000+0  19707-3 0  9991",
        "fallback_line2": "2 25544  51.6336 332.4903 0005031  16.0382 344.0765 15.49743270536903",
    },
    "20580": {
        "name": "HUBBLE SPACE TELESCOPE",
        "color": "#ffb84b",
        "fallback_line1": None,  # TODO: paste a real TLE line 1 here
        "fallback_line2": None,  # TODO: paste a real TLE line 2 here
    },
    "43013": {
        "name": "NOAA 20 (JPSS-1)",
        "color": "#ff4b9d",
        "fallback_line1": None,  # TODO: paste a real TLE line 1 here
        "fallback_line2": None,  # TODO: paste a real TLE line 2 here
    },
    "44713": {
        "name": "STARLINK-1007",
        "color": "#7cff4b",
        "fallback_line1": None,  # TODO: paste a real TLE line 1 here
        "fallback_line2": None,  # TODO: paste a real TLE line 2 here
    },
}

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Constellation Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ts = load.timescale()

# ---------------------------------------------------------------------------
# Per-satellite TLE cache
# ---------------------------------------------------------------------------

_tle_locks: dict = {norad_id: asyncio.Lock() for norad_id in TRACKED_SATELLITES}
_tle_cache: dict = {
    norad_id: {
        "line1": None,
        "line2": None,
        "name": meta["name"],
        "fetched_at": 0.0,
        "source": None,  # "celestrak" or "fallback"
    }
    for norad_id, meta in TRACKED_SATELLITES.items()
}


def _parse_tle_text(text: str, fallback_name: str):
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) >= 3:
        name, line1, line2 = lines[0], lines[1], lines[2]
    elif len(lines) == 2:
        name, line1, line2 = fallback_name, lines[0], lines[1]
    else:
        raise ValueError("Unexpected TLE response format")

    if not (line1.startswith("1 ") and line2.startswith("2 ")):
        raise ValueError("Malformed TLE lines")

    return name, line1, line2


async def _fetch_tle_from_celestrak(norad_id: str, fallback_name: str) -> tuple:
    url = CELESTRAK_TLE_URL.format(norad_id=norad_id)
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return _parse_tle_text(resp.text, fallback_name)


async def get_current_tle(norad_id: str) -> dict | None:
    """
    Per-satellite cached TLE fetch, refreshed at most once per
    TLE_CACHE_SECONDS, guarded by a per-satellite asyncio.Lock.

    Returns None if CelesTrak fails AND there is no cached TLE AND no
    fallback TLE configured for this satellite -- callers must skip the
    satellite in that case rather than fabricate data.
    """
    meta = TRACKED_SATELLITES[norad_id]
    cache = _tle_cache[norad_id]
    lock = _tle_locks[norad_id]

    now = time.time()

    if cache["line1"] is not None and (now - cache["fetched_at"]) < TLE_CACHE_SECONDS:
        return cache

    async with lock:
        now = time.time()
        if cache["line1"] is not None and (now - cache["fetched_at"]) < TLE_CACHE_SECONDS:
            return cache

        try:
            name, line1, line2 = await _fetch_tle_from_celestrak(norad_id, meta["name"])
            cache.update(
                {
                    "line1": line1,
                    "line2": line2,
                    "name": name,
                    "fetched_at": now,
                    "source": "celestrak",
                }
            )
            logger.info(f"[{norad_id}] TLE refreshed from CelesTrak")
        except Exception as exc:
            logger.warning(f"[{norad_id}] CelesTrak fetch failed: {exc}")

            if cache["line1"] is not None:
                # Keep stale-but-real cached TLE, retry CelesTrak next call.
                logger.info(f"[{norad_id}] Keeping stale cached TLE")
                return cache

            if meta["fallback_line1"] and meta["fallback_line2"]:
                cache.update(
                    {
                        "line1": meta["fallback_line1"],
                        "line2": meta["fallback_line2"],
                        "name": meta["name"],
                        "fetched_at": now,
                        "source": "fallback",
                    }
                )
                logger.info(f"[{norad_id}] Using hardcoded fallback TLE")
            else:
                logger.error(
                    f"[{norad_id}] No CelesTrak data, no cache, no fallback -- omitting from response"
                )
                return None

        return cache


def _compute_satellite_state(norad_id: str, tle: dict) -> dict:
    line1, line2, name = tle["line1"], tle["line2"], tle["name"]
    satellite = EarthSatellite(line1, line2, name, ts)

    t = ts.from_datetime(datetime.now(timezone.utc))
    geocentric = satellite.at(t)
    subpoint = geocentric.subpoint()

    velocity_km_s = geocentric.velocity.km_per_s
    speed_km_s = (velocity_km_s[0] ** 2 + velocity_km_s[1] ** 2 + velocity_km_s[2] ** 2) ** 0.5

    return {
        "norad_id": norad_id,
        "name": name,
        "color": TRACKED_SATELLITES[norad_id]["color"],
        "latitude": subpoint.latitude.degrees,
        "longitude": subpoint.longitude.degrees,
        "altitude_km": subpoint.elevation.km,
        "velocity_km_s": speed_km_s,
        "inclination_deg": satellite.model.inclo * 180.0 / 3.14159265358979,
        "tle_epoch": satellite.epoch.utc_iso(),
        "tle_source": tle["source"],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/constellation")
async def get_constellation():
    """
    Returns state for all tracked satellites that currently have usable
    TLE data (live CelesTrak, cached, or fallback). Satellites with no
    data available (CelesTrak down + no cache + no fallback) are omitted
    rather than represented with fabricated values.
    """
    results = []
    skipped = []
    for norad_id in TRACKED_SATELLITES:
        tle = await get_current_tle(norad_id)
        if tle is None:
            skipped.append(norad_id)
            continue
        results.append(_compute_satellite_state(norad_id, tle))

    return {
        "satellites": results,
        "skipped": skipped,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/satellite/{norad_id}")
async def get_satellite(norad_id: str):
    if norad_id not in TRACKED_SATELLITES:
        raise HTTPException(status_code=404, detail=f"NORAD ID {norad_id} is not tracked")
    tle = await get_current_tle(norad_id)
    if tle is None:
        raise HTTPException(
            status_code=503,
            detail=f"No TLE data available for {norad_id} (CelesTrak down, no cache, no fallback)",
        )
    return _compute_satellite_state(norad_id, tle)


@app.get("/api/health")
async def health():
    return {"status": "ok", "tracked": list(TRACKED_SATELLITES.keys())}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
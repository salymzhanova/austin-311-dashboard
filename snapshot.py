"""Live Austin 311 queries and helpers for the district snapshot and request-type detail pages."""
import os
import re
import threading
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://data.austintexas.gov/resource/xwdj-i9he.json"
VIEW_API = "https://data.austintexas.gov/api/views/xwdj-i9he.json"  # dataset metadata (last updated)

AUSTIN_TZ = ZoneInfo("America/Chicago")


def now_austin() -> pd.Timestamp:
    """Current Austin time as a naive timestamp.

    Austin 311 stores times as naive Austin local time (calls peak around 9 AM in the data). The server
    running this app may be on another clock, for example UTC on Streamlit Community Cloud, which is
    5 to 6 hours ahead. Using the server clock would inflate the age of every open request and shift
    every "last N days" window by that much, so all "now" values come from here.
    """
    return pd.Timestamp.now(tz=AUSTIN_TZ).tz_localize(None)


TIMEOUT = (10, 90)  # seconds: connect, read
MAX_PAGES = 20      # hard stop so a bad query can never loop forever
PAGE_SIZE = 50000


class DataUnavailable(RuntimeError):
    """The Austin open data portal could not be reached or returned an error."""


def make_session(backoff: float = 1.5) -> requests.Session:
    """HTTP session that retries rate limits (429) and server errors with growing waits.

    Honors the server's Retry-After header. Set SOCRATA_APP_TOKEN in the environment to use a
    free Socrata app token, which gives a much higher rate limit than anonymous requests.
    """
    retry = Retry(total=4, connect=3, read=3, backoff_factor=backoff,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
                  respect_retry_after_header=True)
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "austin-311-district-dashboard/1.0"
    if os.environ.get("SOCRATA_APP_TOKEN"):
        s.headers["X-App-Token"] = os.environ["SOCRATA_APP_TOKEN"]
    return s


SESSION = make_session()


def fetch_rows(select: str, where: str) -> list[dict]:
    """Page through the 311 endpoint. Raises DataUnavailable after retries are exhausted."""
    rows: list[dict] = []
    try:
        for page in range(MAX_PAGES):
            r = SESSION.get(API, params={
                # sr_number is unique, so rows that share a creation second still have one fixed order
                # and cannot be repeated or skipped at a page boundary.
                "$select": select, "$where": where, "$order": "sr_created_date DESC, sr_number",
                "$limit": PAGE_SIZE, "$offset": page * PAGE_SIZE,
            }, timeout=TIMEOUT)
            r.raise_for_status()
            batch = r.json()
            rows += batch
            if len(batch) < PAGE_SIZE:
                return rows
    except (requests.RequestException, ValueError) as e:
        raise DataUnavailable(f"Austin open data portal request failed: {e.__class__.__name__}") from e
    raise DataUnavailable("Query returned more rows than the safety limit allows.")


@dataclass
class Loaded:
    """A cached result plus when it was fetched and whether it is a stale fallback."""
    data: Any
    fetched_at: datetime
    stale: bool = False


_LAST_GOOD: dict = {}
_LAST_GOOD_LOCK = threading.Lock()


def resilient(key, loader: Callable[[], Any]) -> Loaded:
    """Run `loader`. If the portal is down, return the last good result for `key` marked stale.

    With no earlier good result there is nothing to show, so the error is raised.
    """
    try:
        data = loader()
    except DataUnavailable:
        with _LAST_GOOD_LOCK:
            if key in _LAST_GOOD:
                old = _LAST_GOOD[key]
                return Loaded(old.data, old.fetched_at, stale=True)
        raise
    result = Loaded(data, now_austin().to_pydatetime(), stale=False)
    with _LAST_GOOD_LOCK:
        _LAST_GOOD[key] = result
    return result


def source_last_updated() -> datetime | None:
    """When Austin last updated the 311 dataset itself (None if the metadata call fails)."""
    try:
        r = SESSION.get(VIEW_API, timeout=TIMEOUT)
        r.raise_for_status()
        return datetime.fromtimestamp(r.json()["rowsUpdatedAt"], AUSTIN_TZ).replace(tzinfo=None)
    except (requests.RequestException, ValueError, KeyError):
        return None
# Test, deleted and transferred records are not real service work.
EXCLUDE_STATUS = {"CancelledTesting", "TO BE DELETED", "Transferred"}
OPEN_STATUS = {"Open", "New", "Pending", "Work In Progress", "Duplicate (open)"}

# Bump whenever fetch()/fetch_type() add or change columns. The app passes it to its cached loaders,
# so a running app never serves cached data that lacks the columns the code now expects.
SCHEMA_VERSION = 3  # 3: loaders now return a Loaded(data, fetched_at, stale) wrapper

FIELDS = ["sr_number", "sr_type_desc", "sr_department_desc", "sr_status_desc",
          "sr_created_date", "sr_closed_date", "sr_location", "sr_location_zip_code",
          "sr_location_council_district", "sr_location_lat", "sr_location_long"]


def block_label(loc) -> str:
    """Privacy: turn '7902 SCOTLAND YARD, AUSTIN, TX 78759' into '7900 block Scotland Yard 78759'."""
    if not isinstance(loc, str) or not loc.strip():
        return ""
    z = re.search(r"\b(\d{5})\s*$", loc)
    zip_code = z.group(1) if z else ""
    m = re.match(r"^\s*(\d+)\s+([^,]+)", loc)
    if m:
        return f"{int(m.group(1)) // 100 * 100} block {m.group(2).strip().title()} {zip_code}".strip()
    return f"{loc.split(',')[0].strip().title()} {zip_code}".strip()


def _skip_clause() -> str:
    skip = ",".join(f"'{s}'" for s in EXCLUDE_STATUS)
    return f"sr_status_desc NOT IN ({skip}) AND sr_status_desc NOT LIKE 'Duplicate%'"


def _get(where: str) -> pd.DataFrame:
    df = pd.DataFrame(fetch_rows(",".join(FIELDS), where), columns=FIELDS)
    now = now_austin()
    df["created"] = pd.to_datetime(df["sr_created_date"])
    df["closed"] = pd.to_datetime(df["sr_closed_date"])
    df["is_open"] = df["sr_status_desc"].isin(OPEN_STATUS) & df["closed"].isna()
    df["days_to_close"] = (df["closed"] - df["created"]).dt.total_seconds() / 86400
    df.loc[df["days_to_close"] < 0, "days_to_close"] = pd.NA
    # Wait so far: time to close if closed, otherwise how long it has been open.
    df["wait_days"] = ((df["closed"].fillna(now) - df["created"]).dt.total_seconds() / 86400).clip(lower=0)
    df["open_age_days"] = df["wait_days"].where(df["is_open"])  # age of still-open requests only
    df["district"] = pd.to_numeric(df["sr_location_council_district"], errors="coerce").astype("Int64")
    df["zip"] = df["sr_location_zip_code"]
    df["lat"] = pd.to_numeric(df["sr_location_lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["sr_location_long"], errors="coerce")
    return df


def fetch(district: int, days: int) -> pd.DataFrame:
    """All request types for one district: the last `days` plus the same-length period before."""
    start = now_austin().normalize() - pd.Timedelta(days=2 * days)
    return _get(f"sr_location_council_district='{district}' AND sr_created_date>'{start:%Y-%m-%dT%H:%M:%S}' "
                f"AND {_skip_clause()}")


def fetch_type(type_name: str, days: int) -> pd.DataFrame:
    """One request type, all districts, for the last `days`."""
    start = now_austin().normalize() - pd.Timedelta(days=days)
    safe = type_name.replace("'", "''")
    return _get(f"sr_type_desc='{safe}' AND sr_created_date>'{start:%Y-%m-%dT%H:%M:%S}' AND {_skip_clause()}")


def weekly_with_spikes(created: pd.Series, days: int) -> pd.DataFrame:
    """Complete weeks (ending Sunday) of request counts, with unusually busy weeks flagged.

    A week is a spike when it is well above the period's typical (median) week: more than
    3 robust deviations over it, floored at Poisson noise so small counts are not over-flagged,
    and at least 30% above typical.
    """
    now = now_austin().normalize()
    start = now - pd.Timedelta(days=days)
    counts = created.dt.floor("D").value_counts().rename_axis("day").sort_index()
    weekly = counts.resample("W").sum().rename("requests").reset_index().rename(columns={"day": "week_end"})
    # Keep only full weeks inside the period (drop the partial first and current weeks).
    weekly = weekly[(weekly["week_end"] - pd.Timedelta(days=6) >= start) & (weekly["week_end"] <= now)]
    weekly = weekly.reset_index(drop=True)
    if weekly.empty:
        return weekly.assign(baseline=[], ratio=[], is_spike=[])
    base = float(weekly["requests"].median())
    mad = float((weekly["requests"] - base).abs().median()) * 1.4826
    scale = max(mad, base ** 0.5)
    weekly["baseline"] = base
    weekly["ratio"] = weekly["requests"] / base if base else 0.0
    weekly["is_spike"] = (len(weekly) >= 6) & (weekly["requests"] > base + 3 * scale) & (weekly["ratio"] >= 1.3)
    return weekly


CELL_DEG = 0.002   # about 200 m north-south, so roughly a couple of city blocks
MIN_CELL = 3       # privacy: areas with fewer requests are never shown


def grid_cells(df: pd.DataFrame, bench: float, cell: float = CELL_DEG, min_count: int = MIN_CELL):
    """Group located requests into small grid cells. Returns (cells, number of requests hidden).

    Cells with fewer than `min_count` requests are dropped so a single household cannot be picked out.
    """
    pts = df.dropna(subset=["lat", "lon"]).copy()
    if pts.empty:
        return pts.assign(requests=[]), 0
    pts["gy"] = (pts["lat"] / cell).round().astype(int)
    pts["gx"] = (pts["lon"] / cell).round().astype(int)
    pts["over"] = pts["wait_days"] > bench
    g = pts.groupby(["gy", "gx"]).agg(
        requests=("sr_number", "count"),
        open_now=("is_open", "sum"),
        open_age=("open_age_days", "median"),
        over_pct=("over", lambda s: s.mean() * 100),
        median_days=("days_to_close", "median"),
        zip=("zip", lambda s: s.mode().iat[0] if not s.mode().empty else ""),
    ).reset_index()
    g["lat"], g["lon"] = g["gy"] * cell, g["gx"] * cell
    shown = g[g["requests"] >= min_count].copy()
    hidden = int(g.loc[g["requests"] < min_count, "requests"].sum())
    return shown, hidden


def zip_ranking(df: pd.DataFrame, bench: float, min_count: int = 20) -> pd.DataFrame:
    g = df.dropna(subset=["zip"]).assign(over=lambda x: x["wait_days"] > bench).groupby("zip").agg(
        requests=("sr_number", "count"),
        open_now=("is_open", "sum"),
        open_age=("open_age_days", "median"),
        over_pct=("over", lambda s: s.mean() * 100),
        median_days=("days_to_close", "median"),
    ).reset_index()
    return g[g["requests"] >= min_count].sort_values("over_pct", ascending=False)


HOURS_BELOW_DAYS = 2  # durations shorter than this are shown in hours


def fmt_duration(days) -> str:
    """'2.4 hrs' for anything under 2 days (under an hour: minutes), otherwise '3.1 days'.

    'hrs' is deliberate: 'hours' is wide enough to be cut off ("22.5 ho...") in narrow metric cards.
    """
    if days is None or pd.isna(days):
        return "n/a"
    if days < HOURS_BELOW_DAYS:
        hours = days * 24
        return f"{hours * 60:.0f} min" if hours < 1 else f"{hours:.1f} hrs"
    return f"{days:.1f} days"


def fmt_diff(diff_days: float, ref_days: float) -> str:
    """Signed difference in the same unit the reference value is shown in, e.g. '-7.2 hrs'."""
    if ref_days < HOURS_BELOW_DAYS:
        return f"{diff_days * 24:+.1f} hrs"
    return f"{diff_days:+.1f} days"


def backlog_note(frame: pd.DataFrame, closed_days: pd.Series | None = None) -> str:
    """One line on the age of still-open requests, to tell routine arrivals from stuck ones.

    With `closed_days` (how long closed requests of the same type took), says whether the median open
    request has already waited longer than 9 in 10 closed requests did.
    """
    ages = frame.loc[frame["is_open"], "wait_days"]
    if ages.empty:
        return "Nothing open"
    text = f"Median age: {fmt_duration(ages.median())} · oldest: {fmt_duration(ages.max())}"
    ref = closed_days.dropna() if closed_days is not None else pd.Series(dtype=float)
    if len(ref) >= 20 and len(ages) >= 3:  # a verdict from one or two open tickets would be noise
        p90 = ref.quantile(0.9)
        text += (f" · older than 9 in 10 closed requests took ({fmt_duration(p90)}): possible bottleneck"
                 if ages.median() > p90 else f" · within the normal fix time (9 in 10 close in {fmt_duration(p90)})")
    return text


BOUNDARY_API = "https://data.austintexas.gov/resource/w3v2-cj58.geojson"  # City of Austin council districts


def fetch_district_boundary(district: int) -> list[list[tuple[float, float]]]:
    """Outer boundary rings of one council district as [(lon, lat), ...] lists."""
    try:
        r = SESSION.get(BOUNDARY_API, params={"$where": f"district_number='{int(district)}'"}, timeout=TIMEOUT)
        r.raise_for_status()
        geom = r.json()["features"][0]["geometry"]
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        raise DataUnavailable(f"District boundary request failed: {e.__class__.__name__}") from e
    polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [[(p[0], p[1]) for p in poly[0]] for poly in polygons]  # poly[0] = outer ring, holes ignored


def boundary_bounds(rings) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a set of rings."""
    lons = [p[0] for ring in rings for p in ring]
    lats = [p[1] for ring in rings for p in ring]
    return min(lons), min(lats), max(lons), max(lats)


def fit_view(bounds, width_px: int = 800, height_px: int = 500, fill: float = 0.80) -> tuple[float, float, float]:
    """Map centre and zoom so `bounds` fills `fill` (default 80%) of the map in its limiting direction.

    Uses the Web Mercator maths the map itself uses, so tall and wide districts both fit. The width is
    deliberately on the narrow side because the real container width is not known to Python; on a wider
    screen a wide district simply fills a little less than 80% of the width, never more, so the boundary
    is not clipped.
    """
    import math

    west, south, east, north = bounds
    merc = lambda lat: math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))  # latitude -> mercator y
    span_x = max(math.radians(east - west), 1e-6) / fill  # the map must span this much for the district to fill `fill`
    span_y = max(merc(north) - merc(south), 1e-6) / fill
    zoom = min(math.log2(width_px / 512 * 2 * math.pi / span_x), math.log2(height_px / 512 * 2 * math.pi / span_y))
    centre_y = (merc(north) + merc(south)) / 2
    centre_lat = math.degrees(2 * math.atan(math.exp(centre_y)) - math.pi / 2)
    return centre_lat, (west + east) / 2, max(8.0, min(15.0, zoom))


def outline_trace_points(rings, max_points: int = 800) -> tuple[list, list]:
    """Latitude and longitude lists for drawing the boundary as lines (None separates rings)."""
    lats, lons = [], []
    for ring in rings:
        step = max(1, len(ring) // max_points)
        pts = ring[::step] + [ring[0]]  # thin very detailed outlines, always close the ring
        lons += [p[0] for p in pts] + [None]
        lats += [p[1] for p in pts] + [None]
    return lats, lons


def fetch_city_times(days: int) -> dict:
    """Typical (median) days to resolve for every district and the whole city over the last `days`.

    Same definition as the landing-page card: all request types, requests created in the period
    that have been closed. Returns {"city": float, "by_district": {1: x, ...}}.
    """
    start = now_austin().normalize() - pd.Timedelta(days=days)
    where = (f"sr_created_date>'{start:%Y-%m-%dT%H:%M:%S}' AND sr_closed_date IS NOT NULL AND {_skip_clause()}")
    cols = ["sr_location_council_district", "sr_created_date", "sr_closed_date"]
    df = pd.DataFrame(fetch_rows(",".join(cols), where), columns=cols)
    df["d"] = pd.to_numeric(df["sr_location_council_district"], errors="coerce")
    df["days"] = (pd.to_datetime(df["sr_closed_date"]) - pd.to_datetime(df["sr_created_date"])).dt.total_seconds() / 86400
    df = df[df["days"] >= 0]
    in_districts = df[df["d"].between(1, 10)]
    return {"city": float(df["days"].median()),
            "by_district": {int(k): float(v) for k, v in in_districts.groupby("d")["days"].median().items()}}


def rank_text(mine: float, by_district: dict, district: int) -> str:
    """'Faster than 7 of 9 other districts', comparing medians rounded to 0.1 day."""
    others = [round(v, 1) for d, v in by_district.items() if d != district]
    if not others:
        return ""
    m = round(mine, 1)
    faster, tied = sum(o > m for o in others), sum(o == m for o in others)
    text = f"Faster than {faster} of {len(others)} other districts"
    return text + (f" · tied with {tied}" if tied else "")


def split_periods(df: pd.DataFrame, days: int):
    """Return (current period, previous period) frames."""
    cut = now_austin().normalize() - pd.Timedelta(days=days)
    return df[df["created"] > cut], df[df["created"] <= cut]


def top_types(cur: pd.DataFrame, prev: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    g = cur.groupby("sr_type_desc").agg(
        requests=("sr_number", "count"),
        open_now=("is_open", "sum"),
        open_age=("open_age_days", "median"),
        median_days=("days_to_close", "median"),
        resolved_pct=("is_open", lambda s: (1 - s.mean()) * 100),
    )
    g["previous"] = prev.groupby("sr_type_desc").size().reindex(g.index).fillna(0).astype(int)
    g["change_pct"] = ((g["requests"] - g["previous"]) / g["previous"].where(g["previous"] > 0) * 100).round(0)
    return (g.sort_values("requests", ascending=False).head(n).reset_index()
            .rename(columns={"sr_type_desc": "request_type"}))

"""Austin 311 dashboard for council district office staff.

Snapshot of every request type in a district; click a type to see whether it is getting fixed.
"""
import math
import os
import time

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import snapshot

st.set_page_config(page_title="Austin 311: How is my district served?", page_icon="🏛️", layout="wide")

HIGHLIGHT, MUTED = "#d62728", "#9ecae1"
# The chart toolbar (download, zoom, pan) floats over chart titles in narrow columns, and none of these
# charts need it, so it is hidden. The map keeps its toolbar.
CHART_CONFIG = {"displayModeBar": False}

# --- Map styling -----------------------------------------------------------------------------------
# One basemap: standard OpenStreetMap tiles (no API token). The Carto dark and light basemaps were
# removed because they show attribution watermarks over the map.
BASEMAP = "open-street-map"
# Circle colour scales: teal -> orange for request counts, orange-red -> dark red for the other metrics.
CIRCLE_SCALES = {"count": [[0, "#0b8f8f"], [1, "#f57c00"]], "red": [[0, "#fb6a4a"], [1, "#99000d"]]}
# The district boundary is drawn as a dark line under a light line, so it stands out on the map.
OUTLINE_UNDER, OUTLINE_OVER = "#111111", "#f5f5f5"
MAP_HEIGHT_PX = 500
# The map toolbar (zoom, pan, reset) is switched off for now (CHART_CONFIG hides it). To bring it back,
# pass config={"displayModeBar": True} to the map's st.plotly_chart call; it will then float over the
# top-right corner of the map unless space is reserved for it with a top margin.


def card(col, label: str, value: str, note: str = "", tip: str | None = None) -> None:
    """Compact KPI card: label (with optional hover tooltip), value, and exactly one short note line.

    Every card has the same three rows, so a row of cards has uniform height. Anything longer than a
    few words belongs in `tip` (shown when hovering the "?" beside the label), never in `note`.
    Colour a note with Streamlit markdown, e.g. ":red[text]" or ":green[text]".
    """
    col.metric(label, value, help=tip)
    col.caption(note or "\u00a0")


def persona_banner() -> None:
    """Name who this is for and the question it answers, on every page."""
    st.markdown(
        "**For:** council district office staff preparing for a council member or answering residents.  \n"
        "**Question:** Is my district being served as quickly as the others, and is each kind of request getting fixed?"
    )
    st.divider()


# --- Data refresh -----------------------------------------------------------------------------
# Every query below is cached for CACHE_TTL seconds. Once an entry expires, the next time someone
# opens that view the app quietly fetches fresh data, so the dashboard stays current with no manual
# step. Repeat visits inside the window never touch the Austin servers, which keeps request volume low.
# Override with the DASHBOARD_CACHE_TTL environment variable (seconds; a setting, not a secret). The
# Austin 311 dataset itself is updated through the day, so an hour is a sensible default.
CACHE_TTL = int(os.environ.get("DASHBOARD_CACHE_TTL", 3600))
REFRESH_COOLDOWN = 60  # seconds between manual refreshes, to protect the Austin servers

# Each loader returns a snapshot.Loaded(data, fetched_at, stale). If Austin is unreachable after
# retries, the last good copy is returned flagged stale; errors are never cached.


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading district requests from Austin 311...")
def _snapshot(d: int, n: int, schema: int) -> snapshot.Loaded:
    return snapshot.resilient(("snap", d, n), lambda: snapshot.fetch(d, n))


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading citywide figures from Austin 311...")
def _city_times(n: int, schema: int) -> snapshot.Loaded:
    return snapshot.resilient(("city", n), lambda: snapshot.fetch_city_times(n))


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading this request type from Austin 311...")
def _type(t: str, n: int, schema: int) -> snapshot.Loaded:
    return snapshot.resilient(("type", t, n), lambda: snapshot.fetch_type(t, n))


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)  # district lines change only on redistricting
def _boundary(d: int, schema: int) -> snapshot.Loaded:
    return snapshot.resilient(("boundary", d), lambda: snapshot.fetch_district_boundary(d))


def get_boundary(d: int):
    """The district's boundary rings, or None if the portal can't supply them (the map then falls
    back to fitting the request locations, so a boundary problem never breaks the page)."""
    try:
        return _boundary(d, snapshot.SCHEMA_VERSION).data
    except snapshot.DataUnavailable:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def get_source_updated(schema: int):
    return snapshot.source_last_updated()


loaded_this_run: list[snapshot.Loaded] = []  # what this page view used, for the freshness note


def use(result: snapshot.Loaded):
    loaded_this_run.append(result)
    return result.data


def get_snapshot(d: int, n: int) -> pd.DataFrame:
    return use(_snapshot(d, n, snapshot.SCHEMA_VERSION))


def get_city_times(n: int) -> dict:
    return use(_city_times(n, snapshot.SCHEMA_VERSION))


def get_type(t: str, n: int) -> pd.DataFrame:
    return use(_type(t, n, snapshot.SCHEMA_VERSION))




def load_app_token() -> str | None:
    """Optional Socrata app token from Streamlit secrets. Never hardcode it in the code.

    Locally it comes from .streamlit/secrets.toml (git-ignored); on Streamlit Community Cloud from the
    app's Secrets settings. Without one the app still works, just under Austin's lower anonymous rate limit.
    """
    # Reading st.secrets with no secrets file makes Streamlit print a "No secrets found" message on the
    # page, so check quietly first. load_if_toml_exists() returns False instead of printing.
    try:
        if not st.secrets.load_if_toml_exists():
            return None
        return st.secrets.get("SOCRATA_APP_TOKEN") or None
    except Exception:  # malformed secrets.toml: run without a token rather than break the page
        return None


snapshot.set_app_token(load_app_token())

ss = st.session_state
ss.setdefault("detail_type", None)
ss.setdefault("nav", 0)  # bumped on "back" so selection widgets reset

st.sidebar.title("Austin 311")
district = st.sidebar.selectbox("Council district", list(range(1, 11)), format_func=lambda d: f"District {d}")
st.sidebar.caption("Source: Austin 311 Public Data (data.austintexas.gov). Duplicates, test and transferred "
                   "records are excluded. Locations are shown by block or ZIP code only.")


# ---------------------------------------------------------------- snapshot
def render_snapshot() -> None:
    days = st.sidebar.selectbox("Period", [30, 60, 90], index=0, format_func=lambda n: f"Last {n} days")
    st.title(f"District {district}: 311 snapshot")
    persona_banner()
    snap = get_snapshot(district, days)
    cur, prev = snapshot.split_periods(snap, days)
    st.caption(f"All request types, last {days} days vs the {days} days before. "
               "Pick a request type below to see whether it is getting fixed.")

    delta = (len(cur) - len(prev)) / len(prev) * 100 if len(prev) else None
    done_cur = cur["days_to_close"].dropna()

    s1, s2, s3, s4 = st.columns(4)
    card(s1, "Requests", f"{len(cur):,}",
         f"{'↑' if delta >= 0 else '↓'} {abs(delta):.0f}% vs prior {days} days" if delta is not None else "",
         tip=f"Requests created in the last {days} days, compared with the {days} days before that "
             f"({len(prev):,} requests).")

    open_ages = cur.loc[cur["is_open"], "wait_days"]
    card(s2, "Still open", f"{int(cur['is_open'].sum()):,}",
         f"Median age {snapshot.fmt_duration(open_ages.median())}" if len(open_ages) else "Nothing open",
         tip=snapshot.backlog_note(cur) + ". Age = time since the request was created.")

    if len(done_cur):
        mine_t = done_cur.median()
        city = get_city_times(days)
        diff = mine_t - city["city"]
        citywide = snapshot.fmt_duration(city["city"])
        if abs(diff) < 0.05:
            short, long = "On par with citywide", f"Same as {citywide} citywide."
        else:  # lower is better: green when faster than the city, red when slower
            colour = "green" if diff < 0 else "red"
            short = f":{colour}[{snapshot.fmt_diff(diff, city['city'])}] vs citywide"
            long = f"{snapshot.fmt_diff(diff, city['city'])} vs {citywide} citywide."
        card(s3, "Typical time to resolve", snapshot.fmt_duration(mine_t), short,
             tip=f"{long} {snapshot.rank_text(mine_t, city['by_district'], district)}. "
                 "Median time to close, all request types, requests created in this period that are closed.")
    else:
        card(s3, "Typical time to resolve", "n/a")

    card(s4, "Request types seen", str(cur["sr_type_desc"].nunique()),
         tip="Distinct 311 request types with at least one request in this period.")

    with st.expander("How to read these figures"):
        st.markdown(
            "- **Citywide comparison mixes all request types.** A district with many quick-to-close types "
            "(such as parking enforcement) looks faster. For a like-for-like comparison, open a single "
            "request type below.\n"
            "- **Typical time to resolve** is the median of closed requests, shown in hours when under 2 days. "
            "Requests still open are not counted, so types with many open requests may look faster than they are.\n"
            "- **Median age of open** is how long still-open requests have been waiting. A high number next to "
            "a low typical time points to a stuck backlog.\n"
            "- Duplicates, test and transferred records are excluded. Locations are shown by block or ZIP only.")

    tt = snapshot.top_types(cur, prev)

    st.subheader("Explore a request type in detail")
    others = sorted(set(cur["sr_type_desc"]) - set(tt["request_type"]))
    options = list(tt["request_type"]) + others  # top types first, then everything else
    e1, e2 = st.columns([3, 1], vertical_alignment="bottom")
    pick = e1.selectbox("Request type (top types first, or type to search all)", options, key=f"any_{ss.nav}")
    if e2.button("Open details →", type="primary", use_container_width=True):
        ss.detail_type = pick
        st.rerun()

    st.subheader("Top request types")
    # Health tag from the share resolved (closed) so far; tweak the cut-offs here.
    def health(pct: float) -> str:
        return "Healthy (95%+ resolved)" if pct >= 95 else "Watch (80-95%)" if pct >= 80 else "Needs attention (<80%)"

    def resolved_label(row) -> str:
        pct = round(row["resolved_pct"])
        return f"{min(pct, 99) if row['open_now'] > 0 else pct}% resolved"  # never show 100% while something is open

    chart = tt.sort_values("requests", ascending=False).assign(  # rank 1 first
        label=lambda x: x["request_type"] + "  ·  " + x.apply(resolved_label, axis=1),
        Health=lambda x: x["resolved_pct"].map(health),
    )
    bar = px.bar(chart, x="requests", y="label", orientation="h", text="requests", color="Health",
                 color_discrete_map={"Healthy (95%+ resolved)": "#2ca02c", "Watch (80-95%)": "#ff9f1c",
                                     "Needs attention (<80%)": "#d62728"},
                 category_orders={"label": list(chart["label"])},
                 labels={"requests": "Requests", "label": ""})
    bar.update_traces(textposition="outside", cliponaxis=False, texttemplate="%{x:,}")
    bar.update_layout(height=420, margin=dict(t=10, r=40), legend=dict(orientation="h", y=-0.15, title_text=""),
                      xaxis=dict(range=[0, chart["requests"].max() * 1.15]))
    # Plotly Express already draws the first category_orders entry (rank 1) at the top of a horizontal bar chart.
    st.plotly_chart(bar, use_container_width=True, config=CHART_CONFIG)
    st.caption("Resolved = share of requests in this period that are no longer open. Very recent requests may "
               "not have had time to close, so a lower number is not always a problem. Check the age of open "
               "requests in the table below.")

    shown = tt.assign(median_days=tt["median_days"].map(snapshot.fmt_duration),
                      resolved_pct=tt["resolved_pct"].round(0).where(tt["open_now"] == 0,
                                                                     tt["resolved_pct"].round(0).clip(upper=99)),
                      open_age=tt["open_age"].map(snapshot.fmt_duration)
                      ).rename(columns={"request_type": "Request type", "requests": "Requests",
                                        "previous": "Previous period", "change_pct": "Change (%)",
                                        "open_now": "Open now", "open_age": "Median age of open",
                                        "resolved_pct": "Resolved (%)",
                                        "median_days": "Typical time to resolve"})
    event = st.dataframe(shown, hide_index=True, use_container_width=True, on_select="rerun",
                         selection_mode="single-row", key=f"top_types_{ss.nav}")
    if event.selection.rows:
        ss.detail_type = tt.iloc[event.selection.rows[0]]["request_type"]
        st.rerun()
    st.caption("Typical time to resolve = median of closed requests (shown in hours when under 2 days). "
               "Recent requests still open are not counted, "
               "so types with many open requests may look faster than they are. "
               "Median age of open = how long the still-open requests have been waiting: "
               "a high number next to a low typical time points to a stuck backlog.")

    st.subheader("Weekly volume")
    wk = cur.set_index("created").resample("W").size().rename("requests").reset_index()
    st.plotly_chart(px.bar(wk, x="created", y="requests", labels={"created": "Week ending", "requests": "Requests"},
                           color_discrete_sequence=[MUTED]).update_layout(height=280, margin=dict(t=10)),
                    use_container_width=True, config=CHART_CONFIG)

    st.subheader("Recent activity")
    feed = cur.sort_values("created", ascending=False).head(20).copy()
    feed["Location"] = feed["sr_location"].map(snapshot.block_label)
    feed = feed.rename(columns={"sr_type_desc": "Request type", "created": "Created",
                                "sr_department_desc": "Department", "sr_status_desc": "Status"})
    st.dataframe(feed[["Created", "Request type", "Location", "Department", "Status"]],
                 hide_index=True, use_container_width=True)


# ------------------------------------------------------------------ detail
def bars(x, y, selected, title, ytitle):
    """Peer comparison bars. The selected district stands out three ways: accent colour, a white
    outline, and a bold accent-coloured axis label. Every bar carries its value."""
    is_sel = [d == selected for d in x]
    names = [f"D{d}" for d in x]
    fig = go.Figure(go.Bar(
        x=names, y=y, text=y, texttemplate="%{y}", textposition="outside", cliponaxis=False,
        marker=dict(color=[HIGHLIGHT if s else MUTED for s in is_sel],
                    line=dict(color=["#ffffff" if s else "rgba(0,0,0,0)" for s in is_sel], width=2)),
        hovertemplate="%{x}: %{y}<extra></extra>"))
    fig.update_layout(
        title=title, yaxis_title=ytitle, height=320, margin=dict(t=50, b=10),
        yaxis=dict(range=[0, max(float(max(y)), 0.001) * 1.2]),  # headroom for the value labels
        xaxis=dict(tickmode="array", tickvals=names,
                   ticktext=[f'<span style="color:{HIGHLIGHT}"><b>{n}</b></span>' if s else n
                             for n, s in zip(names, is_sel)]))
    return fig


def render_detail(type_name: str) -> None:
    if st.button("← Back to snapshot"):
        ss.detail_type = None
        ss.nav += 1
        st.rerun()

    st.title(type_name)
    persona_banner()
    st.caption(f"District {district} compared with the other nine districts and the whole city.")

    p1, p2 = st.columns(2)
    periods = {"Last 90 days": 90, "Last 6 months": 180, "Last 12 months": 365}
    days = periods[p1.selectbox("Period", list(periods), index=2)]
    bench = p2.number_input("Target resolution (days)", min_value=1, max_value=30, value=3,
                            help="Austin publishes no resolution target for this data. "
                                 "This target is adjustable and for demonstration only.")

    data = get_type(type_name, days)
    data = data[data["district"].between(1, 10)]
    mine = data[data["district"] == district]
    if mine.empty:
        st.info("No requests of this type in this district for the selected period.")
        return

    st.subheader("Is it getting fixed?")
    done, mine_done = data[data["days_to_close"].notna()], mine[mine["days_to_close"].notna()]
    over_mine = (mine["wait_days"] > bench).mean() * 100
    over_city = (data["wait_days"] > bench).mean() * 100
    med_mine, med_city = mine_done["days_to_close"].median(), done["days_to_close"].median()
    closed_pct = (1 - mine["is_open"].mean()) * 100
    closed_days = mine_done["days_to_close"]
    p90_closed = closed_days.quantile(0.9) if len(closed_days) else float("nan")

    # --- Backlog first: how many are still open, and how long they have been waiting -----------------
    n_open = int(mine["is_open"].sum())
    open_ages = mine.loc[mine["is_open"], "wait_days"]
    if n_open == 0:
        stalled = False
        backlog_note = ":green[No open backlog]"
        backlog_tip = f"Closed rate: {closed_pct:.0f}% · No open requests in this period."
    else:
        median_age = open_ages.median()
        # "Stalled" = the median open request has already waited longer than 9 in 10 closed requests
        # took. Needs enough cases (3+ open, 20+ closed) for the comparison to mean anything.
        stalled = n_open >= 3 and len(closed_days) >= 20 and median_age > p90_closed
        age_text = f"Median age: {snapshot.fmt_duration(median_age)}"
        backlog_note = f":red[⚠ {age_text}]" if stalled else age_text
        backlog_tip = (f"Closed rate: {closed_pct:.0f}% · Oldest open request: "
                       f"{snapshot.fmt_duration(open_ages.max())}")
        if stalled:
            backlog_tip += (f". Warning: the median open request has waited longer than 9 in 10 closed "
                            f"requests took ({snapshot.fmt_duration(p90_closed)}).")

    period_label = next(k for k, v in periods.items() if v == days)
    k1, k2, k3, k4, k5 = st.columns(5)
    card(k1, "Requests", f"{len(mine):,}", period_label,
         tip="Requests of this type created in this district during the selected period.")
    card(k2, "Open now", f"{n_open:,}", backlog_note, tip=backlog_tip)

    diff = med_mine - med_city
    if abs(diff) < 0.05:
        time_note = "On par with city"
    else:  # lower is better: green when faster than the city, red when slower
        time_note = f":{'green' if diff < 0 else 'red'}[{snapshot.fmt_diff(diff, med_city)}] vs city"
    card(k3, "Typical time to resolve", snapshot.fmt_duration(med_mine), time_note,
         tip=f"Median time to close, for closed requests. City: {snapshot.fmt_duration(med_city)}. "
             "Shown in hours (hrs) when under 2 days.")
    card(k4, "90 in 100 fixed within", snapshot.fmt_duration(p90_closed), "",
         tip="9 in 10 closed requests were fixed within this time.")
    over_diff = over_mine - over_city
    over_note = ("On par with city" if abs(over_diff) < 0.5
                 else f":{'green' if over_diff < 0 else 'red'}[{over_diff:+.0f} pts] vs city")
    card(k5, f"Waited over {bench} days", f"{over_mine:.0f}%", over_note,
         tip=f"Share of requests that took, or have so far waited, more than {bench} days: closed requests "
             f"that took longer plus open requests older than that. City: {over_city:.0f}%.")

    with st.expander("How to read these figures"):
        st.markdown(
            "- **Open now** counts requests of this type still open. **Median age** is how long those open "
            "requests have been waiting (time since each was created). It turns red when the median open "
            "request has already waited longer than 9 in 10 closed requests took, a sign the backlog is "
            "stalled. The closed rate and the oldest open request are in the card's tooltip.\n"
            "- **Typical time to resolve** is the median of closed requests only; open requests are not "
            "counted, so it can look good while a backlog grows.\n"
            f"- **Waited over {bench} days** uses the target in the box above (a demo value; Austin publishes "
            "no official target) and includes open requests.\n"
            "- Times under 2 days are shown in hours (hrs).")

    by_d = data.groupby("district").agg(
        median=("days_to_close", "median"),
        over=("wait_days", lambda s: (s > bench).mean() * 100),
    ).reset_index()
    by_d["district"] = by_d["district"].astype(int)
    c1, c2 = st.columns(2)
    in_hours = by_d["median"].max() < snapshot.HOURS_BELOW_DAYS  # same rule as the figures above
    c1.plotly_chart(bars(by_d["district"], (by_d["median"] * (24 if in_hours else 1)).round(1), district,
                         f"Typical time to resolve, by district ({'hrs' if in_hours else 'days'})",
                         "Median hrs" if in_hours else "Median days"),
                    use_container_width=True, config=CHART_CONFIG)
    c2.plotly_chart(bars(by_d["district"], by_d["over"].round(1), district,
                         f"Share that waited over {bench} days, by district", "% of requests"),
                    use_container_width=True, config=CHART_CONFIG)

    cutoffs = [1, 2, 3, 7, 14]
    share = lambda frame: [(frame["days_to_close"] <= c).mean() * 100 for c in cutoffs]
    fast = go.Figure()
    fast.add_bar(x=[f"{c} day{'s' if c > 1 else ''}" for c in cutoffs], y=share(mine_done),
                 name=f"District {district}", marker_color=HIGHLIGHT)
    fast.add_bar(x=[f"{c} day{'s' if c > 1 else ''}" for c in cutoffs], y=share(done), name="City",
                 marker_color=MUTED)
    fast.update_layout(barmode="group", title="How quickly are closed requests fixed?", height=300,
                       yaxis_title="% fixed within", margin=dict(t=70, b=10),
                       legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    st.plotly_chart(fast, use_container_width=True, config=CHART_CONFIG)

    # ------------------------------------------------------------- compare
    st.subheader("Compare two districts")
    rivals = [d for d in range(1, 11) if d != district]
    other = st.selectbox(f"Compare District {district} with", rivals, format_func=lambda d: f"District {d}",
                         key=f"cmp_{type_name}_{district}")

    def stats(d: int) -> dict:
        x = data[data["district"] == d]
        xd = x[x["days_to_close"].notna()]
        return {
            "requests": len(x), "closed_n": len(xd),
            "median": xd["days_to_close"].median(), "p90": xd["days_to_close"].quantile(0.9),
            "over": (x["wait_days"] > bench).mean() * 100, "open": x["is_open"].mean() * 100,
            "open_n": int(x["is_open"].sum()), "open_age": x["open_age_days"].median(),
        }

    sa, sb = stats(district), stats(other)
    if min(sa["closed_n"], sb["closed_n"]) < 20:
        st.info("One of these districts has fewer than 20 closed requests of this type in the period, "
                "so a comparison would not be reliable.")
    else:
        # (label, key, tolerance below which the two are treated as a tie, format). Lower is better for all.
        spec = [("Typical time to resolve", "median", 0.1, snapshot.fmt_duration),
                ("90 in 100 fixed within", "p90", 0.25, snapshot.fmt_duration),
                (f"Waited over {bench} days (%)", "over", 2.0, "{:.0f}"),
                ("Still open (%)", "open", 1.0, "{:.1f}")]
        rows, wins = [], {district: 0, other: 0}
        rows.append({"Measure": "Requests (context, not scored)", f"District {district}": f"{sa['requests']:,}",
                     f"District {other}": f"{sb['requests']:,}", "Doing better": ""})
        age = (lambda s: f"{s['open_n']} open · median age {snapshot.fmt_duration(s['open_age'])}"
               if s["open_n"] else "Nothing open")
        rows.append({"Measure": "Still open: count and median age (context, not scored)",
                     f"District {district}": age(sa), f"District {other}": age(sb), "Doing better": ""})
        for label, key, tol, fmt in spec:
            va, vb = sa[key], sb[key]
            if abs(va - vb) <= tol:
                better = "Tie"
            else:
                winner = district if va < vb else other
                wins[winner] += 1
                better = f"District {winner}"
            show = fmt if callable(fmt) else fmt.format
            rows.append({"Measure": label, f"District {district}": show(va),
                         f"District {other}": show(vb), "Doing better": better})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        if wins[district] == wins[other]:
            st.warning(f"District {district} and District {other} are about even for this request type "
                       f"({wins[district]} measures each).")
        else:
            lead = district if wins[district] > wins[other] else other
            margin = abs(wins[district] - wins[other])
            how = "slightly ahead" if margin == 1 else "doing better"
            st.success(f"District {lead} is {how} on this request type: ahead on {wins[lead]} of "
                       f"{len(spec)} measures (District {district}: {wins[district]}, District {other}: "
                       f"{wins[other]})." + (" The rest are ties." if min(wins.values()) == 0
                                             and wins[lead] < len(spec) else ""))
        st.caption("Lower is better on every scored measure. Differences smaller than a small margin count as a tie "
                   "(0.1 days typical, 0.25 days for the 90th percentile, 2 points waited, 1 point open). "
                   "Percentages are used so a bigger district is not penalized for having more requests.")

        labels = [f"{c} day{'s' if c > 1 else ''}" for c in cutoffs]
        da, db = data[(data["district"] == district) & data["days_to_close"].notna()], \
                 data[(data["district"] == other) & data["days_to_close"].notna()]
        cmp_fig = go.Figure()
        cmp_fig.add_bar(x=labels, y=share(da), name=f"District {district}", marker_color=HIGHLIGHT)
        cmp_fig.add_bar(x=labels, y=share(db), name=f"District {other}", marker_color="#08519c")
        cmp_fig.update_layout(barmode="group", title="Share of closed requests fixed within", height=300,
                              yaxis_title="% fixed within", margin=dict(t=70, b=10),
                              legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(cmp_fig, use_container_width=True, config=CHART_CONFIG)

    # ---------------------------------------------------------------- when
    st.subheader("When are requests coming in?")
    wk = snapshot.weekly_with_spikes(mine["created"], days)
    if len(wk) < 4:
        st.info("Not enough full weeks in this period to show a trend. Try a longer period.")
    else:
        spikes = wk[wk["is_spike"]]
        trend = go.Figure()
        trend.add_bar(x=wk["week_end"], y=wk["requests"], name="Requests per week",
                      marker_color=[HIGHLIGHT if s else MUTED for s in wk["is_spike"]])
        trend.add_scatter(x=wk["week_end"], y=wk["baseline"], mode="lines", name="Typical week",
                          line=dict(color="#555", dash="dash"))
        trend.update_layout(height=320, margin=dict(t=30, b=10), yaxis_title="Requests",
                            xaxis_title="Week ending (Sunday)",
                            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(trend, use_container_width=True, config=CHART_CONFIG)
        if spikes.empty:
            st.success(f"No unusual weeks. Volume stays close to the typical {wk['baseline'].iloc[0]:.0f} "
                       "requests a week.")
        else:
            lines = [f"- Week ending {r.week_end:%b %d}: {int(r.requests)} requests, "
                     f"{r.ratio:.1f}x the typical week ({r.baseline:.0f})" for r in spikes.itertuples()]
            st.warning("**Unusually busy weeks (red bars)**\n" + "\n".join(lines))
            st.caption("A spike means volume was well above this district's typical week. "
                       "The data does not say why. Check holidays, storms, or service changes.")

        dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        dow = mine["created"].dt.day_name().value_counts().reindex(dow_order).fillna(0)
        dfig = go.Figure(go.Bar(x=dow_order, y=dow.values, marker_color=MUTED))
        dfig.update_layout(title="Requests by day of the week", height=280, margin=dict(t=40, b=10),
                           yaxis_title="Requests")
        st.plotly_chart(dfig, use_container_width=True, config=CHART_CONFIG)

    # --------------------------------------------------------------- where
    st.subheader("Where are requests coming from?")
    cells, hidden = snapshot.grid_cells(mine, bench)
    if cells.empty:
        st.info("Not enough located requests to map without singling out a household.")
    else:
        metric_opts = {
            "Number of requests": ("requests", "count"),
            f"Share that waited over {bench} days (%)": ("over_pct", "red"),
            "Still open": ("open_now", "red"),
        }
        choice = st.radio("Color the map by", list(metric_opts), horizontal=True, key=f"mapcolor_{type_name}")
        col, hue = metric_opts[choice]
        scale = CIRCLE_SCALES[hue]
        # Fit the view tightly to the selected district's boundary, so staff see their own blocks and not
        # the whole city. If the boundary is unavailable, fit to the request locations instead.
        rings = get_boundary(district)
        if rings:
            centre_lat, centre_lon, zoom = snapshot.fit_view(snapshot.boundary_bounds(rings))
        else:
            span = max(cells["lat"].max() - cells["lat"].min(), cells["lon"].max() - cells["lon"].min(), 0.02)
            zoom = float(min(13, max(9, math.log2(360 / span) - 1)))
            centre_lat, centre_lon = float(cells["lat"].mean()), float(cells["lon"].mean())
        cells = cells.assign(
            area="Near ZIP " + cells["zip"].astype(str),
            median_days=cells["median_days"].map(snapshot.fmt_duration),
            open_age=cells["open_age"].map(snapshot.fmt_duration),
            over_pct=cells["over_pct"].round(0),
        )
        mfig = px.scatter_mapbox(
            cells, lat="lat", lon="lon", size="requests", color=col, color_continuous_scale=scale,
            size_max=22, zoom=zoom, hover_name="area", opacity=0.88,
            hover_data={"requests": True, "open_now": True, "open_age": True, "over_pct": True,
                        "median_days": True, "lat": False, "lon": False},
            labels={"requests": "Requests", "open_now": "Open", "open_age": "Median age of open",
                    "over_pct": f"% waited over {bench}d", "median_days": "Typical time"},
            center={"lat": centre_lat, "lon": centre_lon},
        )
        if rings:  # draw the district line so the edge of "your area" is clear
            out_lat, out_lon = snapshot.outline_trace_points(rings)
            for width, colour in ((5, OUTLINE_UNDER), (2.2, OUTLINE_OVER)):  # dark casing + light core
                mfig.add_trace(go.Scattermapbox(lat=out_lat, lon=out_lon, mode="lines", hoverinfo="skip",
                                                line=dict(width=width, color=colour), showlegend=False))

        mfig.update_layout(mapbox_style=BASEMAP, height=MAP_HEIGHT_PX, margin=dict(t=0, b=0, l=0, r=0))
        st.plotly_chart(mfig, use_container_width=True, config=CHART_CONFIG)
        st.caption(f"Each circle is a small area of roughly two city blocks, not a single address. Areas "
                   f"with fewer than {snapshot.MIN_CELL} requests are hidden to protect privacy"
                   f"{f' ({hidden:,} requests not shown)' if hidden else ''}.")

    zr = snapshot.zip_ranking(mine, bench)
    if not zr.empty:
        st.markdown(f"**ZIP codes in District {district}, stalled backlog first** (ZIPs with 20+ requests; "
                    f"ZIPs can span more than one district)")
        # Triage order: ZIPs with open requests first, the oldest median open age at the top.
        zr = zr.sort_values(["open_age", "open_now"], ascending=False, na_position="last")

        # One unit per column keeps every figure numeric, so it right-aligns and sorts correctly.
        # Hours (hrs) when the whole column is under 2 days, otherwise days.
        def unit(values: pd.Series) -> tuple[str, float]:
            return ("hrs", 24.0) if values.notna().any() and values.max() < snapshot.HOURS_BELOW_DAYS else ("days", 1.0)

        age_unit, age_k = unit(zr["open_age"])
        typ_unit, typ_k = unit(zr["median_days"])
        c_open, c_age = "Open now", f"Median age of open ({age_unit})"
        c_typ, c_over = f"Typical time ({typ_unit})", f"Waited over {bench} days (%)"
        view = pd.DataFrame({
            "ZIP": zr["zip"], c_open: zr["open_now"].astype(int), c_age: zr["open_age"] * age_k,
            "Requests": zr["requests"], c_typ: zr["median_days"] * typ_k, c_over: zr["over_pct"],
        }).reset_index(drop=True)

        # Backlog severity: any ZIP with open requests is highlighted on its "Open now" and "Median age"
        # cells; red when the median open request has waited longer than 9 in 10 closed requests took.
        stalled_limit = p90_closed * age_k if pd.notna(p90_closed) and len(closed_days) >= 20 else float("inf")
        OPEN_CSS = "background-color: rgba(217,119,6,0.22); color: #d97706; font-weight: 700"
        STALLED_CSS = "background-color: rgba(220,38,38,0.24); color: #dc2626; font-weight: 700"

        def severity(frame: pd.DataFrame) -> pd.DataFrame:
            css = pd.DataFrame("", index=frame.index, columns=frame.columns)
            for i in frame.index:
                if frame.at[i, c_open] > 0:
                    age = frame.at[i, c_age]
                    style = STALLED_CSS if pd.notna(age) and age > stalled_limit else OPEN_CSS
                    css.loc[i, [c_open, c_age]] = style
            return css

        styled = (view.style.apply(severity, axis=None, subset=[c_open, c_age])
                  .format({c_open: "{:,.0f}", c_age: "{:,.1f}", "Requests": "{:,.0f}",
                           c_typ: "{:,.1f}", c_over: "{:,.0f}"}, na_rep="—"))  # em dash, not "n/a"
        st.dataframe(styled, hide_index=True, use_container_width=True)
        st.caption("Amber: the ZIP has open requests. Red: its median open request has waited longer than 9 in 10 "
                   "closed requests took"
                   + (f" ({snapshot.fmt_duration(p90_closed)})." if stalled_limit != float("inf") else ".")
                   + " A dash means nothing is open.")


def refresh_now() -> None:
    """Drop every cached query so the next load goes to Austin. Rate limited per session."""
    wait = REFRESH_COOLDOWN - (time.time() - ss.get("last_refresh", 0))
    if wait > 0:
        st.sidebar.info(f"Data was just refreshed. Try again in {wait:.0f}s.")
        return
    ss.last_refresh = time.time()
    st.cache_data.clear()
    st.rerun()


def render_data_status() -> None:
    """Sidebar note: when the data was fetched, when Austin last updated it, and a refresh button."""
    st.sidebar.divider()
    if loaded_this_run:
        oldest = min(r.fetched_at for r in loaded_this_run)
        st.sidebar.caption(f"Data fetched {oldest:%b %d, %I:%M %p} CT. Refreshes automatically "
                           f"after {CACHE_TTL // 60} minutes.")
        if any(r.stale for r in loaded_this_run):
            st.sidebar.warning("Austin's open data portal did not respond, so some figures are from the last "
                               "successful load shown above. They will update on their own once it is back.")
    updated = get_source_updated(snapshot.SCHEMA_VERSION)
    if updated:
        st.sidebar.caption(f"Austin last updated the 311 dataset {updated:%b %d, %I:%M %p} CT.")
    if st.sidebar.button("Refresh data now"):
        refresh_now()


try:
    if ss.detail_type:
        render_detail(ss.detail_type)
    else:
        render_snapshot()
    render_data_status()
except snapshot.DataUnavailable as err:
    st.error("The Austin open data portal is not responding right now, and there is no earlier copy to show. "
             "This is usually temporary (the portal limits how often it can be queried).")
    st.caption(str(err))
    if st.button("Try again"):
        refresh_now()
    st.stop()

st.divider()
st.caption("Data sourced from the [City of Austin Open Data Portal]"
           "(https://data.austintexas.gov/Utilities-and-City-Services/Austin-311-Public-Data/xwdj-i9he) "
           "(Austin 311 Public Data).")

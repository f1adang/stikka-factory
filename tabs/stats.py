"""Statistics tab content - show print statistics over time.

Charts and tables here are hand-rolled HTML rather than st.line_chart /
st.bar_chart / st.table. Those widgets serialise through pandas and pyarrow,
whose native code is what crashed the app with SIGILL on the Raspberry Pi
print hosts. A SIGILL kills the process outright - Python cannot catch it -
so wrapping them in try/except (as this tab used to) offers no protection at
all. Not calling them is the only thing that works.
"""

import html
import logging
import streamlit as st
from datetime import datetime, timedelta

logger = logging.getLogger("sticker_factory.tabs.stats")

RANGES = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "All time": None,
}


def _humanise_age(then):
    """Render how long ago `then` was, in the largest sensible unit."""
    delta = datetime.now() - then
    if delta < timedelta(0):
        # Clock skew, or a record written by a host whose time was wrong.
        return "just now"
    total = int(delta.total_seconds())
    if total >= 86400:
        days = total // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"
    if total >= 3600:
        hours = total // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if total >= 60:
        mins = total // 60
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    return "just now"


def _bar_chart(rows, color):
    """A horizontal bar chart as plain HTML. rows: [(label, count), ...]."""
    if not rows:
        return ""
    peak = max(count for _, count in rows) or 1
    bars = []
    for label, count in rows:
        width = count / peak * 100
        bars.append(
            f'<div class="stikka-bar-row">'
            f'<div class="stikka-bar-label" title="{html.escape(str(label))}">'
            f"{html.escape(str(label))}</div>"
            f'<div class="stikka-bar-track">'
            f'<div class="stikka-bar-fill" style="width:{width:.4f}%;background:{color}"></div>'
            f"</div>"
            f'<div class="stikka-bar-value">{count}</div>'
            f"</div>"
        )
    return f'<div class="stikka-chart">{"".join(bars)}</div>'


def _table(headers, rows):
    """A plain HTML table. Avoids st.table, which serialises through Arrow."""
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f'<table class="stikka-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


_STYLE = """
<style>
.stikka-chart { margin: 0.25rem 0 1rem 0; }
.stikka-bar-row { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.3rem; }
.stikka-bar-label {
    flex: 0 0 11rem; font-size: 0.85rem; opacity: 0.85;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.stikka-bar-track { flex: 1 1 auto; background: rgba(128,128,128,0.16); border-radius: 3px; height: 1.1rem; }
.stikka-bar-fill { height: 100%; border-radius: 3px; min-width: 2px; }
.stikka-bar-value { flex: 0 0 3rem; text-align: right; font-variant-numeric: tabular-nums; font-size: 0.85rem; }
.stikka-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.stikka-table th, .stikka-table td {
    text-align: left; padding: 0.35rem 0.6rem;
    border-bottom: 1px solid rgba(128,128,128,0.22);
}
.stikka-table th { opacity: 0.7; font-weight: 600; }
.stikka-table td:last-child, .stikka-table th:last-child {
    text-align: right; font-variant-numeric: tabular-nums;
}
</style>
"""


def render():
    """Render the Statistics tab."""
    st.subheader(":chart_with_upwards_trend: Print Statistics")

    try:
        from stats_utils import get_dashboard_stats
        data = get_dashboard_stats()
    except Exception as e:
        logger.error(f"Failed to load stats: {e}", exc_info=True)
        st.error(f"Statistics are not available: {e}")
        return

    accent = st.get_option("theme.primaryColor") or "#9673ff"
    st.markdown(_STYLE, unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Prints", data["total_prints"])
    col2.metric("Printers Used", len(data["printers"]))
    if data["last_print"]:
        col3.metric("Last Print", _humanise_age(datetime.fromisoformat(data["last_print"])))
    else:
        col3.metric("Last Print", "Never")

    by_date = data["by_date"]
    if not by_date:
        st.info("No print statistics available yet. Start printing to see statistics!")
        return

    selected_range = st.selectbox("Time range", list(RANGES), key="stats_date_range")
    days = RANGES[selected_range]
    cutoff = (datetime.now().date() - timedelta(days=days)) if days else None

    filtered = {
        date: printers
        for date, printers in by_date.items()
        if cutoff is None or datetime.strptime(date, "%Y-%m-%d").date() >= cutoff
    }
    if not filtered:
        st.info(f"No prints in the selected time range ({selected_range}).")
        return

    dates = sorted(filtered)

    st.subheader("Prints Over Time")
    st.markdown(
        _bar_chart([(d, sum(filtered[d].values())) for d in dates], accent),
        unsafe_allow_html=True,
    )

    st.subheader("Total Prints by Printer")
    # Totals for the selected range, not all time, so the two sections agree.
    range_totals = {}
    for printers in filtered.values():
        for name, count in printers.items():
            range_totals[name] = range_totals.get(name, 0) + count
    ranked = sorted(range_totals.items(), key=lambda kv: kv[1], reverse=True)
    st.markdown(_bar_chart(ranked, accent), unsafe_allow_html=True)

    with st.expander("Detailed statistics"):
        st.markdown("**By printer** (all time)")
        all_time = sorted(data["printers"].items(), key=lambda kv: kv[1], reverse=True)
        st.markdown(_table(["Printer", "Total prints"], all_time), unsafe_allow_html=True)

        st.markdown("**By day**")
        st.markdown(
            _table(
                ["Date", "Prints"],
                [(d, sum(filtered[d].values())) for d in reversed(dates)],
            ),
            unsafe_allow_html=True,
        )

        if data["first_print"]:
            first = datetime.fromisoformat(data["first_print"])
            st.caption(f"Recording since {first:%Y-%m-%d %H:%M}")

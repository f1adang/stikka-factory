"""Statistics tab content - show print statistics over time."""

import logging
import streamlit as st
from datetime import datetime, timedelta

logger = logging.getLogger("sticker_factory.tabs.stats")

# Lazy import stats_utils to avoid import-time issues
def _get_stats_functions():
    """Lazy import stats functions."""
    try:
        from stats_utils import get_stats_by_date, get_total_stats, get_stats_summary
        return get_stats_by_date, get_total_stats, get_stats_summary
    except Exception as e:
        logger.error(f"Failed to import stats_utils: {e}", exc_info=True)
        raise


def render():
    """Render the Statistics tab."""
    st.subheader(":chart_with_upwards_trend: Print Statistics")
    
    # Lazy import stats functions
    try:
        get_stats_by_date, get_total_stats, get_stats_summary = _get_stats_functions()
    except Exception as e:
        st.error(f"Statistics module is not available: {e}")
        st.info("Statistics tracking may be disabled or unavailable.")
        return
    
    # Get summary stats
    summary = get_stats_summary()
    
    # Display summary cards
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Prints", summary["total_prints"])
    with col2:
        printer_count = len(summary["printers"])
        st.metric("Printers Used", printer_count)
    with col3:
        if summary["last_print"]:
            last_print_dt = datetime.fromisoformat(summary["last_print"])
            time_ago = datetime.now() - last_print_dt
            if time_ago.days > 0:
                st.metric("Last Print", f"{time_ago.days} days ago")
            elif time_ago.seconds > 3600:
                st.metric("Last Print", f"{time_ago.seconds // 3600} hours ago")
            else:
                st.metric("Last Print", f"{time_ago.seconds // 60} minutes ago")
        else:
            st.metric("Last Print", "Never")
    
    # Get stats by date
    date_stats = get_stats_by_date()
    
    if not date_stats:
        st.info("No print statistics available yet. Start printing to see statistics!")
        return
    
    # Date range selector
    st.subheader("Time Range")
    date_range_options = ["Last 7 days", "Last 30 days", "Last 90 days", "All time"]
    selected_range = st.selectbox("Select time range", date_range_options, key="stats_date_range")
    
    # Filter by date range
    if selected_range == "Last 7 days":
        cutoff_date = datetime.now() - timedelta(days=7)
    elif selected_range == "Last 30 days":
        cutoff_date = datetime.now() - timedelta(days=30)
    elif selected_range == "Last 90 days":
        cutoff_date = datetime.now() - timedelta(days=90)
    else:
        cutoff_date = None
    
    # Filter date_stats by cutoff_date
    filtered_date_stats = {}
    for date_str, printers in date_stats.items():
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        if cutoff_date is None or date_obj >= cutoff_date.date():
            filtered_date_stats[date_str] = printers
    
    if not filtered_date_stats:
        st.info(f"No prints in the selected time range ({selected_range}).")
        return
    
    # Prepare data for line chart (no pandas needed!)
    # Streamlit charts work with dicts: {date: {printer: count}}
    chart_data = {}
    all_printers = set()
    
    # Collect all printer names
    for printers in filtered_date_stats.values():
        all_printers.update(printers.keys())
    
    # Build chart data structure: {date: {printer1: count1, printer2: count2}}
    sorted_dates = sorted(filtered_date_stats.keys())
    for date_str in sorted_dates:
        chart_data[date_str] = {}
        for printer in all_printers:
            chart_data[date_str][printer] = filtered_date_stats[date_str].get(printer, 0)
    
    # Line chart - Streamlit can handle dict of dicts
    st.subheader("Prints Over Time")
    try:
        st.line_chart(chart_data, use_container_width=True)
    except Exception as e:
        logger.warning(f"Error rendering line chart: {e}")
        # Fallback to simple display
        st.subheader("Daily Print Counts")
        for date_str in sorted_dates:
            total_for_date = sum(filtered_date_stats[date_str].values())
            st.write(f"**{date_str}**: {total_for_date} prints")
    
    # Printer totals
    st.subheader("Total Prints by Printer")
    totals = get_total_stats()
    
    if totals:
        # Create bar chart data - Streamlit accepts dict directly
        totals_sorted = dict(sorted(totals.items(), key=lambda x: x[1], reverse=True))
        try:
            st.bar_chart(totals_sorted, use_container_width=True)
        except Exception as e:
            logger.warning(f"Error rendering bar chart: {e}")
        
        # Show table with details (using Streamlit's native table)
        with st.expander("View Detailed Statistics"):
            table_data = [{"Printer": name, "Total Prints": count} 
                          for name, count in totals_sorted.items()]
            st.table(table_data)
    
    # Daily breakdown table
    st.subheader("Daily Breakdown")
    daily_totals_list = []
    for date_str in sorted(filtered_date_stats.keys(), reverse=True):
        total_for_date = sum(filtered_date_stats[date_str].values())
        daily_totals_list.append({"Date": date_str, "Total Prints": total_for_date})
    
    st.table(daily_totals_list)


"""Statistics tab content - show print statistics over time."""

import logging
import streamlit as st
from datetime import datetime, timedelta
from stats_utils import get_stats_by_date, get_total_stats, get_stats_summary

logger = logging.getLogger("sticker_factory.tabs.stats")


def render():
    """Render the Statistics tab."""
    st.subheader(":chart_with_upwards_trend: Print Statistics")
    
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
    
    # Try to import pandas, but handle gracefully if it fails
    try:
        import pandas as pd
        pandas_available = True
    except Exception as e:
        st.error(f"Pandas is not available: {e}")
        st.info("Showing basic statistics without charts.")
        pandas_available = False
    
    if not pandas_available:
        # Fallback: show basic stats without pandas
        st.subheader("Daily Print Counts")
        for date_str in sorted(date_stats.keys(), reverse=True):
            total_for_date = sum(date_stats[date_str].values())
            st.write(f"**{date_str}**: {total_for_date} prints")
        return
    
    # Convert to DataFrame for easier manipulation
    df_data = []
    for date_str, printers in date_stats.items():
        for printer_name, count in printers.items():
            df_data.append({
                "date": datetime.strptime(date_str, "%Y-%m-%d"),
                "printer": printer_name,
                "count": count
            })
    
    try:
        df = pd.DataFrame(df_data)
    except Exception as e:
        st.error(f"Error creating DataFrame: {e}")
        logger.error(f"Error creating DataFrame: {e}", exc_info=True)
        # Fallback: show basic stats
        st.subheader("Daily Print Counts")
        for date_str in sorted(date_stats.keys(), reverse=True):
            total_for_date = sum(date_stats[date_str].values())
            st.write(f"**{date_str}**: {total_for_date} prints")
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
    
    if cutoff_date:
        df = df[df["date"] >= cutoff_date]
    
    if df.empty:
        st.info(f"No prints in the selected time range ({selected_range}).")
        return
    
    try:
        # Group by date and printer, sum counts
        df_grouped = df.groupby(["date", "printer"])["count"].sum().reset_index()
        
        # Pivot for line chart
        df_pivot = df_grouped.pivot(index="date", columns="printer", values="count").fillna(0)
        
        # Sort by date
        df_pivot = df_pivot.sort_index()
        
        # Line chart
        st.subheader("Prints Over Time")
        st.line_chart(df_pivot, use_container_width=True)
        
        # Printer totals
        st.subheader("Total Prints by Printer")
        totals = get_total_stats()
        
        if totals:
            # Create a DataFrame for the bar chart
            totals_df = pd.DataFrame([
                {"Printer": name, "Total Prints": count}
                for name, count in sorted(totals.items(), key=lambda x: x[1], reverse=True)
            ])
            
            st.bar_chart(totals_df.set_index("Printer"), use_container_width=True)
            
            # Show table with details
            with st.expander("View Detailed Statistics"):
                st.dataframe(totals_df, use_container_width=True, hide_index=True)
        
        # Daily breakdown table
        st.subheader("Daily Breakdown")
        daily_totals = df.groupby("date")["count"].sum().reset_index()
        daily_totals.columns = ["Date", "Total Prints"]
        daily_totals = daily_totals.sort_values("Date", ascending=False)
        st.dataframe(daily_totals, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"Error displaying charts: {e}")
        logger.error(f"Error displaying charts: {e}", exc_info=True)
        # Fallback: show basic stats
        st.subheader("Daily Print Counts")
        for date_str in sorted(date_stats.keys(), reverse=True):
            total_for_date = sum(date_stats[date_str].values())
            st.write(f"**{date_str}**: {total_for_date} prints")


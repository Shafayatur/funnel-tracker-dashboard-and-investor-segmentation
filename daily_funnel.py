"""
WeGro — Daily Funnel Tracker
-----------------------------
Ingests the IR team's manually-maintained daily tracking excel/CSV (one
row per day: registrations, tickets booked, tickets invested, investment
value, etc.) and renders the funnel/trend dashboard + PDF export.

This module owns everything for this one page. app.py just calls
`render()` - same pattern as investor_segments.py.
"""

from datetime import datetime
import io

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

from pdf_utils import render_pdf_export_section
import sheets_source

# Expected columns from the IR team's daily tracking sheet.
# Keys = canonical internal name, Values = possible header text variants
# (IR team's manual sheet formatting can drift, so we match loosely).
EXPECTED_COLUMNS = {
    "day": ["day"],
    "registrations": ["number of registration (interest)", "number of registration", "registration"],
    "tickets_booked": ["number of tickets booked (consideration)", "tickets booked"],
    "tickets_invested": ["number of tickets invested", "tickets invested"],
    "unique_investors": ["number of unique investors", "unique investors"],
    "new_investors": ["number of new unique investors", "new unique investors"],
    "old_investors": ["number of old unique investors", "old unique investors"],
    "investment_value": ["investment in value (payment)", "investment in value", "investment value"],
    "reinvestment": ["reinvestment"],
    "payables": ["payables"],
}


# --------------------------------------------------------------------------
# Data loading & cleaning
# --------------------------------------------------------------------------

def normalize_header(col: str) -> str:
    return str(col).strip().lower()


def map_columns(df: pd.DataFrame) -> dict:
    """Map actual sheet headers to canonical internal names."""
    normalized = {normalize_header(c): c for c in df.columns}
    mapping = {}
    for canonical, variants in EXPECTED_COLUMNS.items():
        for variant in variants:
            if variant in normalized:
                mapping[canonical] = normalized[variant]
                break
    return mapping


@st.cache_data(show_spinner=False)
def load_raw(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Load a file with NO header assumption - every row (including any
    section labels like 'Cumulative' above the real header) becomes a data
    row. We detect the real header row separately, since manually-built
    sheets sometimes have a label row above the actual column names."""
    if filename.lower().endswith(".csv"):
        for encoding in ["utf-8", "utf-8-sig", "latin1"]:
            try:
                return pd.read_csv(io.BytesIO(file_bytes), header=None, encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("Could not decode the CSV file with common encodings (utf-8, latin1).")
    return pd.read_excel(io.BytesIO(file_bytes), header=None)


def detect_header_row(raw_df: pd.DataFrame, max_scan: int = 10) -> int:
    """Scan the first `max_scan` rows and return the index of the row that
    best matches our known column headers (e.g. contains 'day', 'registration',
    etc). Falls back to row 0 if nothing matches well."""
    all_known_variants = [v for variants in EXPECTED_COLUMNS.values() for v in variants]

    best_row, best_score = 0, -1
    for i in range(min(max_scan, len(raw_df))):
        row_values = [normalize_header(v) for v in raw_df.iloc[i].tolist()]
        score = sum(1 for val in row_values if val in all_known_variants)
        if score > best_score:
            best_row, best_score = i, score

    return best_row


def build_dataframe(raw_df: pd.DataFrame, header_row: int) -> pd.DataFrame:
    """Slice a headerless dataframe into a proper dataframe using the given
    row index as the header. Drops fully-empty trailing columns (common
    Excel artifact) and de-duplicates any remaining blank/duplicate headers
    so downstream display (st.dataframe) never sees duplicate column names.

    Works positionally (iloc) throughout, since the raw header row can
    contain multiple blank/NaN entries - selecting by label in that case
    would return a DataFrame instead of a Series (ambiguous truth value)."""
    header_values = raw_df.iloc[header_row].tolist()
    data = raw_df.iloc[header_row + 1:].copy()
    data = data.reset_index(drop=True)

    # Decide which column positions to keep - drop columns with no header
    # AND no data at all (blank trailing columns left over from Excel).
    keep_positions = []
    for pos in range(data.shape[1]):
        header_val = header_values[pos]
        header_is_blank = pd.isna(header_val) or str(header_val).strip() == ""
        column_is_empty = data.iloc[:, pos].isna().all()
        if header_is_blank and column_is_empty:
            continue
        keep_positions.append(pos)

    data = data.iloc[:, keep_positions]
    kept_headers = [header_values[pos] for pos in keep_positions]

    # De-duplicate any remaining blank or repeated headers (e.g. a blank
    # column that does have stray data, or two columns with the same name)
    # so pandas/Streamlit never see duplicate column labels.
    seen = {}
    new_columns = []
    for header_val in kept_headers:
        label = "Unnamed" if (pd.isna(header_val) or str(header_val).strip() == "") else str(header_val)
        if label in seen:
            seen[label] += 1
            label = f"{label}_{seen[label]}"
        else:
            seen[label] = 0
        new_columns.append(label)
    data.columns = new_columns

    return data


def clean_data(df: pd.DataFrame, col_map: dict) -> pd.DataFrame:
    """Rename to canonical columns, parse dates, coerce numerics, drop empty rows."""
    rename_dict = {v: k for k, v in col_map.items()}
    df = df.rename(columns=rename_dict)

    if "day" not in df.columns:
        raise ValueError(
            "Could not find a 'Day' column in the uploaded file. "
            "Please check the sheet has a column named 'Day'."
        )

    # Parse dates - primary format matches the IR team's sheet: "01-Aug-26"
    raw_day_values = df["day"].copy()
    df["day"] = pd.to_datetime(df["day"], format="%d-%b-%y", errors="coerce")
    # Fallback for rows that don't match the expected format (e.g. manual edits)
    still_missing = df["day"].isna()
    if still_missing.any():
        df.loc[still_missing, "day"] = pd.to_datetime(
            raw_day_values[still_missing], errors="coerce", dayfirst=True
        )
    df = df.dropna(subset=["day"])

    numeric_cols = [
        "registrations", "tickets_booked", "tickets_invested",
        "unique_investors", "new_investors", "old_investors",
        "investment_value", "reinvestment", "payables",
    ]
    for col in numeric_cols:
        if col in df.columns:
            # Manually-typed cells sometimes include comma thousand-
            # separators (e.g. "7,722,295"), which pd.to_numeric can't
            # parse - it silently becomes NaN with errors="coerce" and
            # then vanishes from every downstream .sum(), with no error
            # ever surfacing. Strip commas first so both plain numbers
            # (720000) and comma-formatted text ("7,722,295") parse the
            # same way.
            cleaned = df[col].astype(str).str.replace(",", "", regex=False).str.strip()
            df[col] = pd.to_numeric(cleaned, errors="coerce")

    # Keep only the columns the dashboard actually knows about - anything
    # else in the sheet (target tables, stray notes, extra columns) is
    # dropped here rather than silently carried through to charts/tables.
    known_columns = ["day"] + numeric_cols
    df = df[[c for c in known_columns if c in df.columns]]

    df = df.sort_values("day").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Chart builders
# --------------------------------------------------------------------------

def render_metric_rows(metrics: list, max_per_row: int = 4):
    """Renders a list of (label, value) pairs as st.metric cards, wrapping
    into multiple rows of at most `max_per_row` columns each - avoids
    cramming 6-7 metrics into one line, which truncates labels on
    narrower screens. Used by both kpi_row() and comparison_kpi_row()."""
    for i in range(0, len(metrics), max_per_row):
        chunk = metrics[i:i + max_per_row]
        cols = st.columns(len(chunk))
        for col, (label, value) in zip(cols, chunk):
            col.metric(label, value)


def kpi_row(df: pd.DataFrame) -> dict:
    """Renders the KPI metric row and returns the computed values as a
    dict, so the same numbers can be reused in the PDF export without
    recalculating them."""
    total_reg = df.get("registrations", pd.Series(dtype=float)).sum()
    total_booked = df.get("tickets_booked", pd.Series(dtype=float)).sum()
    total_invested = df.get("tickets_invested", pd.Series(dtype=float)).sum()
    total_value = df.get("investment_value", pd.Series(dtype=float)).sum()
    total_unique = df.get("unique_investors", pd.Series(dtype=float)).sum()
    total_new = df.get("new_investors", pd.Series(dtype=float)).sum()

    conversion_rate = (total_invested / total_reg * 100) if total_reg else 0
    new_investor_pct = (total_new / total_unique * 100) if total_unique else 0

    # Reinvestment is optional - only present once the IR team's sheet
    # includes the new column. Kept out of the metrics list when absent
    # so older files without it still render exactly as before.
    has_reinvestment = "reinvestment" in df.columns
    total_reinvestment = df.get("reinvestment", pd.Series(dtype=float)).sum() if has_reinvestment else 0

    metrics = [
        ("Total Registrations", f"{total_reg:,.0f}"),
        ("Tickets Booked", f"{total_booked:,.0f}"),
        ("Tickets Invested", f"{total_invested:,.0f}"),
        ("Investment Value", f"Tk {total_value:,.0f}"),
        ("Reg → Invested Conv.", f"{conversion_rate:.1f}%"),
        ("New Investor Mix", f"{new_investor_pct:.1f}%"),
    ]
    if has_reinvestment:
        metrics.append(("Total Reinvestment", f"Tk {total_reinvestment:,.0f}"))

    render_metric_rows(metrics)

    result = {
        "Total Registrations": f"{total_reg:,.0f}",
        "Tickets Booked": f"{total_booked:,.0f}",
        "Tickets Invested": f"{total_invested:,.0f}",
        "Investment Value": f"Tk {total_value:,.0f}",
        "Reg → Invested Conversion": f"{conversion_rate:.1f}%",
        "New Investor Mix": f"{new_investor_pct:.1f}%",
    }
    if has_reinvestment:
        result["Total Reinvestment"] = f"Tk {total_reinvestment:,.0f}"
    return result


def funnel_chart(df: pd.DataFrame):
    stages = ["Registration (Interest)", "Tickets Booked (Consideration)", "Tickets Invested (Conversion)"]
    values = [
        df.get("registrations", pd.Series(dtype=float)).sum(),
        df.get("tickets_booked", pd.Series(dtype=float)).sum(),
        df.get("tickets_invested", pd.Series(dtype=float)).sum(),
    ]
    fig = go.Figure(go.Funnel(
        y=stages,
        x=values,
        textinfo="value+percent initial",
        marker={"color": ["#4C72B0", "#55A868", "#C44E52"]},
    ))
    fig.update_layout(title="Investor Funnel (Period Total)", height=380)
    return fig


def trend_chart(df: pd.DataFrame):
    fig = go.Figure()
    for col, label, color in [
        ("registrations", "Registrations", "#4C72B0"),
        ("tickets_booked", "Tickets Booked", "#55A868"),
        ("tickets_invested", "Tickets Invested", "#C44E52"),
    ]:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["day"], y=df[col], mode="lines+markers", name=label, line=dict(color=color)))
    fig.update_layout(title="Daily Funnel Trend", xaxis_title="Day", yaxis_title="Count", height=400)
    return fig


def investor_mix_chart(df: pd.DataFrame):
    if "new_investors" not in df.columns or "old_investors" not in df.columns:
        return None
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["day"], y=df["new_investors"], name="New Investors", marker_color="#55A868"))
    fig.add_trace(go.Bar(x=df["day"], y=df["old_investors"], name="Old Investors", marker_color="#8C8C8C"))
    fig.update_layout(barmode="stack", title="New vs Old Unique Investors (Daily)", height=380)
    return fig


def investor_mix_donut(df: pd.DataFrame):
    """Period-total New vs Old investor split as a donut chart. Valid pie
    chart candidate since New + Old are mutually exclusive and sum to the
    whole (Unique Investors) - unlike the funnel stages, which overlap.
    The Unique Investors total is shown as text in the donut's center
    hole, since it's the sum of the two slices, not a third slice itself."""
    if "new_investors" not in df.columns or "old_investors" not in df.columns:
        return None
    total_new = df["new_investors"].sum()
    total_old = df["old_investors"].sum()
    total_unique = total_new + total_old
    if total_unique == 0:
        return None
    fig = go.Figure(go.Pie(
        labels=["New Investors", "Old Investors"],
        values=[total_new, total_old],
        hole=0.5,
        marker=dict(colors=["#55A868", "#8C8C8C"]),
        textinfo="label+percent+value",
        domain=dict(x=[0, 1], y=[0, 0.82]),  # reserve top ~18% so outside labels never collide with the title
    ))
    fig.update_layout(
        title=dict(text="New vs Old Investor Mix (Period Total)", y=0.98, yanchor="top"),
        height=440,
        margin=dict(t=70, b=30),
        annotations=[dict(
            text=f"{total_unique:,.0f}<br>Total",
            x=0.5, y=0.41,
            font_size=20,
            showarrow=False,
        )],
    )
    return fig


def investment_vs_target_chart(df: pd.DataFrame, monthly_target: float):
    if "investment_value" not in df.columns:
        return None
    df = df.copy()
    df["cumulative_value"] = df["investment_value"].fillna(0).cumsum()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["day"], y=df["cumulative_value"], mode="lines+markers",
        name="Cumulative Investment Value", line=dict(color="#4C72B0"),
        fill="tozeroy",
    ))
    if monthly_target > 0:
        fig.add_hline(
            y=monthly_target, line_dash="dash", line_color="red",
            annotation_text=f"Target: Tk {monthly_target:,.0f}", annotation_position="top left",
        )
    fig.update_layout(title="Cumulative Investment Value vs Monthly Target", xaxis_title="Day", yaxis_title="Value (Tk)", height=400)
    return fig


def registrations_chart(df: pd.DataFrame):
    if "registrations" not in df.columns or df["registrations"].dropna().empty:
        return None
    total = df["registrations"].sum()
    fig = px.bar(
        df,
        x="day",
        y="registrations",
        title=f"Daily Registrations (Total: {total:,.0f})",
        color_discrete_sequence=["#4C72B0"],
        text="registrations",
    )
    fig.update_traces(textposition="outside")
    fig.update_layout(height=350, xaxis_title="Day", yaxis_title="Registrations")
    return fig


def payables_chart(df: pd.DataFrame):
    if "payables" not in df.columns or df["payables"].dropna().empty:
        return None
    # Explicit color - see note in period_investment_comparison_chart on
    # why px charts need an explicit color to export correctly as PDF images.
    fig = px.bar(df, x="day", y="payables", title="Payables (Daily)", color_discrete_sequence=["#C44E52"])
    fig.update_layout(height=350)
    return fig


def reinvestment_chart(df: pd.DataFrame):
    """Reinvestment is typically populated once per month (blank on most
    days), not a daily-accumulating figure like Investment Value - so this
    renders as sparse bars, one tall bar on the day it was recorded rather
    than a continuous series. That sparsity is expected, not a data bug."""
    if "reinvestment" not in df.columns or df["reinvestment"].dropna().empty:
        return None
    fig = px.bar(df, x="day", y="reinvestment", title="Reinvestment (Daily)", color_discrete_sequence=["#D4A017"])
    fig.update_layout(height=350)
    return fig


def pick_date_from_data(available_days: list, label: str, default_idx: int, key_prefix: str):
    """Year -> Month -> Day dropdown picker (side by side) built only from
    dates that actually exist in the uploaded data. Avoids Streamlit's
    calendar date_input widget, which can lock up when the data spans a
    year boundary and no single month exists in both years."""
    st.sidebar.markdown(f"**{label}**")
    col_year, col_month, col_day = st.sidebar.columns(3)

    years = sorted({d.year for d in available_days})
    default_year = available_days[default_idx].year
    with col_year:
        year = st.selectbox(
            "Year", years, index=years.index(default_year), key=f"{key_prefix}_year"
        )

    months_for_year = sorted({d.month for d in available_days if d.year == year})
    month_names = {m: datetime(2000, m, 1).strftime("%b") for m in months_for_year}  # short: Jan, Feb...
    default_month = available_days[default_idx].month
    default_month = default_month if default_month in months_for_year else months_for_year[0]
    with col_month:
        month = st.selectbox(
            "Month", months_for_year, index=months_for_year.index(default_month),
            format_func=lambda m: month_names[m], key=f"{key_prefix}_month",
        )

    days_for_month = sorted({d.day for d in available_days if d.year == year and d.month == month})
    default_day = available_days[default_idx].day
    default_day = default_day if default_day in days_for_month else days_for_month[0]
    with col_day:
        day = st.selectbox(
            "Day", days_for_month, index=days_for_month.index(default_day), key=f"{key_prefix}_day"
        )

    return datetime(year, month, day).date()


def get_period_key_and_label(d, granularity: str):
    """Given a date and a granularity ('Daily', 'Monthly', 'Yearly'),
    return a (grouping_key, display_label) pair."""
    if granularity == "Daily":
        return (d.year, d.month, d.day), d.strftime("%d-%b-%y")
    elif granularity == "Yearly":
        return (d.year,), str(d.year)
    else:  # Monthly
        return (d.year, d.month), datetime(d.year, d.month, 1).strftime("%b %Y")


def build_period_summary(full_df: pd.DataFrame, selected_keys: list, granularity: str) -> pd.DataFrame:
    """Aggregate totals per selected period (day, month, or year), for
    comparing periods against each other side by side."""
    rows = []
    for key in selected_keys:
        if granularity == "Daily":
            year, month, day = key
            period_df = full_df[
                (full_df["day"].dt.year == year) & (full_df["day"].dt.month == month) & (full_df["day"].dt.day == day)
            ]
            label = datetime(year, month, day).strftime("%d-%b-%y")
        elif granularity == "Yearly":
            (year,) = key
            period_df = full_df[full_df["day"].dt.year == year]
            label = str(year)
        else:  # Monthly
            year, month = key
            period_df = full_df[(full_df["day"].dt.year == year) & (full_df["day"].dt.month == month)]
            label = datetime(year, month, 1).strftime("%b %Y")

        if period_df.empty:
            continue
        row = {"period_label": label}
        for col in ["registrations", "tickets_booked", "tickets_invested",
                    "unique_investors", "new_investors", "old_investors",
                    "investment_value", "reinvestment", "payables"]:
            if col in period_df.columns:
                row[col] = period_df[col].sum()
        rows.append(row)
    return pd.DataFrame(rows)


def compute_derived_metrics(summary_df: pd.DataFrame) -> pd.DataFrame:
    """Conversion rates, avg ticket size, and unique investors carried
    through from period totals - kept separate from the raw totals table
    so that table stays clean; these feed the extra comparison charts."""
    derived = summary_df[["period_label"]].copy()

    reg = summary_df.get("registrations", pd.Series(0, index=summary_df.index)).replace(0, pd.NA)
    booked = summary_df.get("tickets_booked", pd.Series(0, index=summary_df.index)).replace(0, pd.NA)
    invested = summary_df.get("tickets_invested", pd.Series(0, index=summary_df.index)).replace(0, pd.NA)
    value = summary_df.get("investment_value", pd.Series(0, index=summary_df.index))

    derived["reg_to_booked_pct"] = (summary_df.get("tickets_booked", 0) / reg * 100).fillna(0)
    derived["booked_to_invested_pct"] = (summary_df.get("tickets_invested", 0) / booked * 100).fillna(0)
    derived["reg_to_invested_pct"] = (summary_df.get("tickets_invested", 0) / reg * 100).fillna(0)
    derived["unique_investors"] = summary_df.get("unique_investors", 0)
    derived["avg_ticket_size"] = (value / invested).fillna(0)

    return derived


def build_period_change_table(summary_df: pd.DataFrame):
    """Period-over-period % change for the core volume/value metrics.
    Returns None if fewer than 2 periods are selected (nothing to diff)."""
    if len(summary_df) < 2:
        return None
    metrics = [c for c in ["registrations", "tickets_booked", "tickets_invested",
                            "unique_investors", "investment_value", "reinvestment"] if c in summary_df.columns]
    change_df = summary_df[["period_label"] + metrics].copy()
    for m in metrics:
        change_df[f"{m} Δ%"] = change_df[m].pct_change().mul(100).round(1)
    return change_df


def period_funnel_comparison_chart(summary_df: pd.DataFrame, granularity: str):
    """Grouped bar chart comparing Registration/Booked/Invested totals
    across selected periods."""
    fig = go.Figure()
    for col, label, color in [
        ("registrations", "Registrations", "#4C72B0"),
        ("tickets_booked", "Tickets Booked", "#55A868"),
        ("tickets_invested", "Tickets Invested", "#C44E52"),
    ]:
        if col in summary_df.columns:
            fig.add_trace(go.Bar(x=summary_df["period_label"], y=summary_df[col], name=label, marker_color=color))
    fig.update_layout(
        barmode="group", title=f"Funnel Comparison Across {granularity} Periods",
        height=420, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Count",
    )
    return fig


def period_investment_comparison_chart(summary_df: pd.DataFrame, granularity: str):
    """Bar chart comparing total Investment Value across selected periods."""
    if "investment_value" not in summary_df.columns:
        return None
    # Explicit color instead of px.bar's default colorway - Streamlit
    # registers its own 'streamlit' plotly template globally, which only
    # resolves to real colors inside a live browser session. Relying on
    # the default colorway here would silently bake in broken placeholder
    # colors (renders as solid black) whenever the chart is exported to a
    # static image outside that context, e.g. for the PDF report.
    fig = px.bar(
        summary_df, x="period_label", y="investment_value",
        title=f"Investment Value Comparison Across {granularity} Periods",
        color_discrete_sequence=["#4C72B0"],
    )
    fig.update_layout(height=420, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Investment Value (Tk)")
    return fig


def period_registrations_comparison_chart(summary_df: pd.DataFrame, granularity: str):
    if "registrations" not in summary_df.columns:
        return None
    total = summary_df["registrations"].sum()
    fig = px.bar(
        summary_df,
        x="period_label",
        y="registrations",
        title=f"Registrations Comparison Across {granularity} Periods (Total: {total:,.0f})",
        color_discrete_sequence=["#4C72B0"],
        text="registrations",
    )
    fig.update_traces(textposition="outside")
    fig.update_layout(
        height=420,
        xaxis_title=granularity[:-2] if granularity != "Daily" else "Day",
        yaxis_title="Registrations",
    )
    return fig


def period_reinvestment_comparison_chart(summary_df: pd.DataFrame, granularity: str):
    """Bar chart comparing total Reinvestment across selected periods."""
    if "reinvestment" not in summary_df.columns or summary_df["reinvestment"].dropna().empty:
        return None
    fig = px.bar(
        summary_df, x="period_label", y="reinvestment",
        title=f"Reinvestment Comparison Across {granularity} Periods",
        color_discrete_sequence=["#D4A017"],
    )
    fig.update_layout(height=420, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Reinvestment (Tk)")
    return fig


def add_stack_total_labels(fig, x_labels, totals):
    """Adds a text annotation above each bar in a stacked chart showing the
    combined total (sum of all segments) - stacked bars only show each
    segment's own value on hover/inline, not the at-a-glance grand total,
    so this makes the total readable without hovering."""
    annotations = list(fig.layout.annotations) if fig.layout.annotations else []
    for x_val, total in zip(x_labels, totals):
        annotations.append(dict(
            x=x_val, y=total, text=f"{total:,.0f}", showarrow=False,
            yshift=12, font=dict(size=12, color="#FFFFFF"),
        ))
    fig.update_layout(annotations=annotations)


def period_total_investment_stack_chart(summary_df: pd.DataFrame, granularity: str):
    """Stacked bar chart: Investment Value + Reinvestment, so each bar's
    total height is the combined Total Investment for that period - same
    stacking pattern as the New vs Old Investors chart. Only meaningful
    when Reinvestment is present (Monthly/Yearly granularity); on Daily
    granularity Reinvestment has no daily figure, so this quietly returns
    None rather than showing a misleading single-segment bar."""
    if "investment_value" not in summary_df.columns:
        return None
    if "reinvestment" not in summary_df.columns or summary_df["reinvestment"].dropna().empty:
        return None
    fig = go.Figure()
    fig.add_trace(go.Bar(x=summary_df["period_label"], y=summary_df["investment_value"], name="Investment Value", marker_color="#4C72B0"))
    fig.add_trace(go.Bar(x=summary_df["period_label"], y=summary_df["reinvestment"], name="Reinvestment", marker_color="#D4A017"))
    fig.update_layout(
        barmode="stack", title=f"Total Investment (Investment Value + Reinvestment) Across {granularity} Periods",
        height=420, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Total Investment (Tk)",
    )
    totals = summary_df["investment_value"].fillna(0) + summary_df["reinvestment"].fillna(0)
    add_stack_total_labels(fig, summary_df["period_label"], totals)
    return fig


def period_investor_mix_comparison_chart(summary_df: pd.DataFrame, granularity: str):
    """Stacked bar chart comparing New vs Old Unique Investors across
    selected periods."""
    if "new_investors" not in summary_df.columns or "old_investors" not in summary_df.columns:
        return None
    fig = go.Figure()
    fig.add_trace(go.Bar(x=summary_df["period_label"], y=summary_df["new_investors"], name="New Investors", marker_color="#55A868"))
    fig.add_trace(go.Bar(x=summary_df["period_label"], y=summary_df["old_investors"], name="Old Investors", marker_color="#8C8C8C"))
    fig.update_layout(
        barmode="stack", title=f"New vs Old Investors Across {granularity} Periods",
        height=420, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Unique Investors",
    )
    totals = summary_df["new_investors"].fillna(0) + summary_df["old_investors"].fillna(0)
    add_stack_total_labels(fig, summary_df["period_label"], totals)
    return fig


def period_conversion_rate_chart(derived_df: pd.DataFrame, granularity: str):
    """Grouped bar comparing funnel-stage conversion rates (%) across
    selected periods - shows whether the funnel got more/less efficient,
    not just whether volumes moved."""
    fig = go.Figure()
    for col, label, color in [
        ("reg_to_booked_pct", "Reg → Booked %", "#4C72B0"),
        ("booked_to_invested_pct", "Booked → Invested %", "#55A868"),
        ("reg_to_invested_pct", "Reg → Invested %", "#C44E52"),
    ]:
        fig.add_trace(go.Bar(x=derived_df["period_label"], y=derived_df[col], name=label, marker_color=color))
    fig.update_layout(
        barmode="group", title=f"Funnel Conversion Rates Across {granularity} Periods",
        height=420, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Conversion %",
    )
    return fig


def period_unique_investors_chart(derived_df: pd.DataFrame, granularity: str):
    """Bar chart of total Unique Investors per period - a single clean
    total line, complementing the existing New vs Old stacked view."""
    fig = go.Figure(go.Bar(x=derived_df["period_label"], y=derived_df["unique_investors"], marker_color="#4C72B0"))
    fig.update_layout(
        title=f"Total Unique Investors Across {granularity} Periods",
        height=380, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Unique Investors",
    )
    return fig


def period_avg_ticket_size_chart(derived_df: pd.DataFrame, granularity: str):
    """Bar chart of average investment value per invested ticket, per
    period - flags whether investment value growth is from more investors
    or bigger tickets."""
    fig = go.Figure(go.Bar(x=derived_df["period_label"], y=derived_df["avg_ticket_size"], marker_color="#D4A017"))
    fig.update_layout(
        title=f"Average Ticket Size Across {granularity} Periods",
        height=380, xaxis_title=granularity[:-2] if granularity != "Daily" else "Day", yaxis_title="Avg Investment Value per Ticket (Tk)",
    )
    return fig


def comparison_kpi_row(summary_df: pd.DataFrame) -> dict:
    """KPI cards for Compare Periods mode - aggregated across the selected
    periods. Mirrors kpi_row()'s pattern for the date-range view, and its
    return dict feeds the PDF export the same way (previously an empty
    dict, so comparison-mode PDFs had no KPI cards at all)."""
    total_reg = summary_df.get("registrations", pd.Series(dtype=float)).sum()
    total_invested = summary_df.get("tickets_invested", pd.Series(dtype=float)).sum()
    total_value = summary_df.get("investment_value", pd.Series(dtype=float)).sum()
    total_unique = summary_df.get("unique_investors", pd.Series(dtype=float)).sum()

    conversion_rate = (total_invested / total_reg * 100) if total_reg else 0
    avg_ticket = (total_value / total_invested) if total_invested else 0

    has_reinvestment = "reinvestment" in summary_df.columns
    total_reinvestment = summary_df.get("reinvestment", pd.Series(dtype=float)).sum() if has_reinvestment else 0

    metrics = [
        ("Total Registrations", f"{total_reg:,.0f}"),
        ("Total Tickets Invested", f"{total_invested:,.0f}"),
        ("Total Investment Value", f"Tk {total_value:,.0f}"),
        ("Overall Conversion", f"{conversion_rate:.1f}%"),
        ("Avg Ticket Size", f"Tk {avg_ticket:,.0f}"),
    ]
    if has_reinvestment:
        metrics.append(("Total Reinvestment", f"Tk {total_reinvestment:,.0f}"))

    render_metric_rows(metrics, max_per_row=3)

    result = {
        "Total Registrations": f"{total_reg:,.0f}",
        "Total Tickets Invested": f"{total_invested:,.0f}",
        "Total Investment Value": f"Tk {total_value:,.0f}",
        "Overall Conversion": f"{conversion_rate:.1f}%",
        "Avg Ticket Size": f"Tk {avg_ticket:,.0f}",
    }
    if has_reinvestment:
        result["Total Reinvestment"] = f"Tk {total_reinvestment:,.0f}"
    return result


# --------------------------------------------------------------------------
# Daily Funnel Tracker tab
# --------------------------------------------------------------------------

def render():
    st.title("📊 WeGro — CF Update Tracker Dashboard (IR)")
    st.caption("Internal use only.")

    data_source = st.sidebar.radio("Data source", ["Live Google Sheet", "Upload file"])

    if data_source == "Live Google Sheet":
        st.sidebar.caption("Live data may be up to 5 minutes stale.")
        if st.sidebar.button("🔄 Refresh now"):
            st.cache_data.clear()
            st.rerun()
        try:
            headerless_df = sheets_source.load_sheet_raw(
                st.secrets["ir_master_files_url"], "CF Update Tracker"
            )
        except Exception as e:
            st.error(f"Could not load the Google Sheet. Details: {e}")
            return
    else:
        uploaded_file = st.file_uploader(
            "CF Update Tracker file (.xlsx, .xls, or .csv)", type=["xlsx", "xls", "csv"], key="funnel_uploader"
        )

        if not uploaded_file:
            st.info("Waiting for a file to be uploaded.")
            return

        try:
            headerless_df = load_raw(uploaded_file.getvalue(), uploaded_file.name)
        except Exception as e:
            st.error(f"Could not read the uploaded file. Make sure it's a valid Excel or CSV file. Details: {e}")
            return

    if headerless_df.empty:
        st.error("The uploaded file appears to be empty.")
        return

    # Auto-detect which row holds the real column headers (handles sheets
    # with a label row like "Cumulative" above the actual headers).
    detected_row = detect_header_row(headerless_df)

    with st.expander("File preview — confirm the header row", expanded=False):
        st.caption(
            "If your sheet has extra label rows above the real column names "
            "(e.g. a 'Cumulative' row), pick the correct header row below."
        )
        preview_rows = min(10, len(headerless_df))
        st.dataframe(headerless_df.head(preview_rows), use_container_width=True)
        header_row = st.number_input(
            "Which row (0 = first row) contains the column names?",
            min_value=0, max_value=preview_rows - 1, value=detected_row, step=1,
        )

    raw_df = build_dataframe(headerless_df, header_row)
    col_map = map_columns(raw_df)

    if "day" not in col_map:
        st.error(
            "Could not find a 'Day' column in this file. "
            f"Columns found: {list(raw_df.columns)}"
        )
        return

    try:
        df = clean_data(raw_df, col_map)
    except Exception as e:
        st.error(f"Error while processing the data: {e}")
        return

    if df.empty:
        st.warning("No valid rows found after parsing dates. Please check the 'Day' column format.")
        return

    # Sidebar controls
    st.sidebar.header("Filters")
    min_day, max_day = df["day"].min().date(), df["day"].max().date()
    available_days = sorted(df["day"].dt.date.unique())
    full_df = df.copy()  # keep the unfiltered data around for month comparison mode

    filter_mode = st.sidebar.radio(
        "Select by",
        ["Date range", "Specific dates", "Compare periods"],
        help="'Date range' picks all days between a start and end. "
             "'Specific dates' lets you pick individual, non-consecutive days. "
             "'Compare periods' shows multiple days, months, or years side by side.",
    )

    if filter_mode == "Compare periods":
        granularity = st.sidebar.radio("Compare by", ["Daily", "Monthly", "Yearly"], horizontal=True, index=1)

        # Build the available period keys/labels for the chosen granularity
        period_map = {}
        for d in available_days:
            key, label = get_period_key_and_label(d, granularity)
            period_map[label] = key
        # Sort labels chronologically by their underlying key, not alphabetically
        sorted_labels = [label for label, _ in sorted(period_map.items(), key=lambda kv: kv[1])]

        default_selection = sorted_labels[-2:] if len(sorted_labels) >= 2 else sorted_labels
        selected_labels = st.sidebar.multiselect(
            f"Pick {granularity.lower()} periods to compare", options=sorted_labels, default=default_selection,
        )

        if not selected_labels:
            st.info(f"Pick at least one {granularity.lower()[:-2] if granularity != 'Daily' else 'day'} in the sidebar to compare.")
            return

        # Sort chronologically by the underlying (year, month, ...) key,
        # not by the order the user clicked them in the multiselect.
        selected_keys = sorted(period_map[label] for label in selected_labels)
        summary_df = build_period_summary(full_df, selected_keys, granularity)

        if summary_df.empty:
            st.warning("No data found for the selected periods.")
            return

        st.subheader(f"{granularity} Comparison — Summary")
        comparison_kpis = comparison_kpi_row(summary_df)
        derived_df = compute_derived_metrics(summary_df)

        st.divider()
        comparison_figures = []

        st.subheader("Funnel Volume")
        funnel_fig = period_funnel_comparison_chart(summary_df, granularity)
        st.plotly_chart(funnel_fig, use_container_width=True)
        comparison_figures.append(funnel_fig)

        st.subheader("Registrations")
        registrations_comparison_fig = period_registrations_comparison_chart(summary_df, granularity)
        if registrations_comparison_fig:
            st.plotly_chart(registrations_comparison_fig, use_container_width=True)
            comparison_figures.append(registrations_comparison_fig)

        st.subheader("Funnel Efficiency")
        conv_fig = period_conversion_rate_chart(derived_df, granularity)
        st.plotly_chart(conv_fig, use_container_width=True)
        comparison_figures.append(conv_fig)

        st.subheader("Investment")
        total_invest_fig = period_total_investment_stack_chart(summary_df, granularity)
        if total_invest_fig:
            st.plotly_chart(total_invest_fig, use_container_width=True)
            comparison_figures.append(total_invest_fig)
        else:
            invest_fig = period_investment_comparison_chart(summary_df, granularity)
            if invest_fig:
                st.plotly_chart(invest_fig, use_container_width=True)
                comparison_figures.append(invest_fig)

        st.subheader("Investor Mix")
        mix_fig = period_investor_mix_comparison_chart(summary_df, granularity)
        if mix_fig:
            st.plotly_chart(mix_fig, use_container_width=True)
            comparison_figures.append(mix_fig)

        uniq_fig = period_unique_investors_chart(derived_df, granularity)
        st.plotly_chart(uniq_fig, use_container_width=True)
        comparison_figures.append(uniq_fig)

        st.subheader("Ticket Economics")
        ticket_fig = period_avg_ticket_size_chart(derived_df, granularity)
        st.plotly_chart(ticket_fig, use_container_width=True)
        comparison_figures.append(ticket_fig)

        st.divider()
        st.subheader(f"{granularity} Totals")
        st.dataframe(summary_df, use_container_width=True)

        change_df = build_period_change_table(summary_df)
        if change_df is not None:
            st.subheader(f"{granularity}-over-{granularity} % Change")
            st.dataframe(change_df, use_container_width=True)
        else:
            st.caption("Select at least 2 periods to see period-over-period % change.")

        csv = summary_df.to_csv(index=False).encode("utf-8")
        st.download_button(f"Download {granularity.lower()} comparison as CSV", csv, f"ir_{granularity.lower()}_comparison.csv", "text/csv")

        st.divider()
        render_pdf_export_section(
            title="WeGro — CF Update Tracker Dashboard",
            subtitle=f"{granularity} Comparison: {', '.join(selected_labels)}",
            kpi_dict=comparison_kpis,
            figures=comparison_figures,
            table_df=summary_df,
            key_prefix="compare",
            file_prefix="ir_dashboard",
        )
        return  # comparison mode is a distinct view - skip the day-level charts below

    if filter_mode == "Date range":
        # Streamlit's calendar date_input can lock up when data spans a
        # year boundary and no single month exists in both years (e.g. Aug
        # only exists in 2026, Oct only exists in 2025 - neither year's
        # dropdown can be reached from the other). Year -> Month -> Day
        # dropdowns built only from real data avoid that entirely.
        start = pick_date_from_data(available_days, "Start date", default_idx=0, key_prefix="start")
        end = pick_date_from_data(available_days, "End date", default_idx=-1, key_prefix="end")

        if start > end:
            st.sidebar.error("Start date is after end date - showing no data. Adjust the dates above.")
            df = df.iloc[0:0]
        else:
            df = df[(df["day"].dt.date >= start) & (df["day"].dt.date <= end)]
    else:
        selected_days = st.sidebar.multiselect(
            "Pick specific dates",
            options=available_days,
            default=[],
            format_func=lambda d: d.strftime("%d-%b-%y"),
            help="Leave empty to show all dates. Pick one or more to narrow down.",
        )
        if selected_days:
            df = df[df["day"].dt.date.isin(selected_days)]
        else:
            st.sidebar.caption(f"Showing all {len(available_days)} dates. Pick above to narrow down.")

    st.sidebar.header("Monthly Target")
    monthly_target = st.sidebar.number_input(
        "Investment value target (Tk)", min_value=0.0, value=0.0, step=100000.0,
        help="Enter the monthly investment value target manually. Used for the pacing chart.",
    )

    if df.empty:
        st.warning("No data in the selected date range.")
        return

    # KPIs
    kpi_values = kpi_row(df)
    st.divider()

    # Charts - kept in a list too, so the same figures can be reused in the PDF export
    report_figures = []

    c1, c2 = st.columns(2)
    with c1:
        funnel_fig = funnel_chart(df)
        st.plotly_chart(funnel_fig, use_container_width=True)
        report_figures.append(funnel_fig)
    with c2:
        donut_fig = investor_mix_donut(df)
        if donut_fig:
            st.plotly_chart(donut_fig, use_container_width=True)
            report_figures.append(donut_fig)
        else:
            st.info("New/Old investor columns not found in this file.")

    mix_fig = investor_mix_chart(df)
    if mix_fig:
        st.plotly_chart(mix_fig, use_container_width=True)
        report_figures.append(mix_fig)

    trend_fig = trend_chart(df)
    st.plotly_chart(trend_fig, use_container_width=True)
    report_figures.append(trend_fig)

    registrations_fig = registrations_chart(df)
    if registrations_fig:
        st.plotly_chart(registrations_fig, use_container_width=True)
        report_figures.append(registrations_fig)

    invest_target_fig = investment_vs_target_chart(df, monthly_target)
    st.plotly_chart(invest_target_fig, use_container_width=True)
    report_figures.append(invest_target_fig)

    pay_fig = payables_chart(df)
    if pay_fig:
        st.plotly_chart(pay_fig, use_container_width=True)
        report_figures.append(pay_fig)

    reinvest_fig = reinvestment_chart(df)
    if reinvest_fig:
        st.plotly_chart(reinvest_fig, use_container_width=True)
        report_figures.append(reinvest_fig)

    st.divider()
    st.subheader("Raw Data")
    st.dataframe(df, use_container_width=True)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Download filtered data as CSV", csv, "ir_daily_filtered.csv", "text/csv")

    st.divider()
    render_pdf_export_section(
        title="WeGro — CF Update Tracker Dashboard",
        subtitle=f"Period: {df['day'].min().strftime('%d %b %Y')} to {df['day'].max().strftime('%d %b %Y')}",
        kpi_dict=kpi_values,
        figures=report_figures,
        key_prefix="daterange",
        file_prefix="ir_dashboard",
    )
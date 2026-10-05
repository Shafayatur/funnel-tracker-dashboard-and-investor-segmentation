"""
WeGro Investor Segmentation
---------------------------
Ingests the IR team's raw order export (one row per order/investment) and
produces investor-level segmentation: tiers (Low/Mid/High/VIP), favorite
product category, preferred tenure, activity status, and best-performing
product+tenure combinations.

This mirrors the Kaggle notebook pipeline 1:1 (Cells 1-9), just wrapped as
reusable functions so it can run inside Streamlit instead of Kaggle.
"""

import io
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from pdf_utils import render_pdf_export_section

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

VALID_STATUS = ["invested", "disbursement_running", "closed"]
RUNNING_STATUS = ["invested", "disbursement_running"]

DATE_COLS = [
    "customer_created_at", "order_created_at", "invested_created_at",
    "returned_created_at", "close_date", "bank_attachment_date",
]

TIER_ORDER = ["VIP", "High", "Mid", "Low"]

# Keyword -> category map (same logic as Cell 3 in the notebook).
# Order matters: first matching keyword wins, so more specific categories
# (e.g. "Goat") should stay above broader ones if overlap is ever an issue.
PRODUCT_CATEGORIES = {
    "Cattle":          ["cattle", "bull", "calf", "buffalo", "qurbani", "lamb"],
    "Poultry":         ["poultry", "duck", "chicken", "egg trade", "sonali"],
    "Fish":            ["fish", "crab", "tilapia", "hilsha", "pangas", "shrimp", "pabda"],
    "Rice/Paddy":      ["rice", "paddy", "boro", "aman"],
    "Maize/Corn":      ["maize", "corn"],
    "Potato":          ["potato"],
    "Onion":           ["onion"],
    "Spices":          ["chilli", "chili", "turmeric", "mustard"],
    "Jute":            ["jute"],
    "Fruit":           ["mango", "watermelon", "seasonal fruit"],
    "Dairy":           ["dairy", "cheese", "molasses", "honey", "milk"],
    "Goat":            ["goat"],
    "Vegetable":       ["vegetable", "tomato", "cauliflower", "cucumber", "eggplant",
                         "okra", "dherosh", "pumpkin", "garlic", "kochu", "taro"],
    "Commodity Trade": ["commodity"],
    "Agri Input":      ["agricultural input", "fertilizer", "pesticide", "micronutrient",
                         "seed import", "agri machinery", "silage", "seed processing", "seed"],
    "Meat Processing": ["meat processing"],
}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_raw_file(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Load the raw order export as-is (CSV or Excel), header assumed on row 0
    since this is a system export, not a manually-formatted sheet."""
    if filename.lower().endswith(".csv"):
        for encoding in ["utf-8", "utf-8-sig", "cp1252", "latin1"]:
            try:
                return pd.read_csv(io.BytesIO(file_bytes), encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("Could not decode the CSV file with common encodings.")
    return pd.read_excel(io.BytesIO(file_bytes))


REQUIRED_COLUMNS = [
    "status", "customer_unique_id", "customer_name", "base_grand_total",
    "project_name", "tenure", "order_created_at", "invested_created_at", "id",
]


def validate_columns(df: pd.DataFrame) -> list:
    """Return a list of any required columns missing from the upload."""
    return [c for c in REQUIRED_COLUMNS if c not in df.columns]


# --------------------------------------------------------------------------
# Cleaning / preprocessing (mirrors notebook Cell 3)
# --------------------------------------------------------------------------

def categorize_product(name: str) -> str:
    name = str(name).lower()
    for category, keywords in PRODUCT_CATEGORIES.items():
        for kw in keywords:
            if kw in name:
                return category
    return "Other"


def preprocess(df: pd.DataFrame, start_date: pd.Timestamp | None,
               end_date: pd.Timestamp | None = None) -> pd.DataFrame:
    """Filter to valid status, convert dates, tag product category, and
    optionally filter to a start/end date range (based on order_created_at,
    which is always fully populated - see notebook discussion on why).
    The end date is inclusive of the whole day."""
    df = df[df["status"].isin(VALID_STATUS)].copy()

    for col in DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", dayfirst=True)

    if start_date is not None:
        df = df[df["order_created_at"] >= start_date].copy()

    if end_date is not None:
        # Inclusive end date: keep everything before midnight of the next day,
        # so orders with a time component on the end date itself are kept.
        df = df[df["order_created_at"] < end_date + pd.Timedelta(days=1)].copy()

    df["product_category"] = df["project_name"].apply(categorize_product)

    # Maturity date: when did/will this investment's capital become free?
    # Prefer the real close_date; fall back to invested_created_at + tenure
    # (months) when close_date is missing, so investors aren't flagged
    # inactive while their money is still locked in a running investment.
    df["maturity_date"] = df["close_date"]
    needs_fallback = df["maturity_date"].isna() & df["invested_created_at"].notna() & df["tenure"].notna()
    df.loc[needs_fallback, "maturity_date"] = (
        df.loc[needs_fallback, "invested_created_at"]
        + pd.to_timedelta(df.loc[needs_fallback, "tenure"].astype(float) * 30, unit="D")
    )
    return df


# --------------------------------------------------------------------------
# Investor-level aggregation (mirrors notebook Cells 4-9)
# --------------------------------------------------------------------------

def assign_tier(amount: float) -> str:
    if amount < 50_000:
        return "Low"
    elif amount < 250_000:
        return "Mid"
    elif amount < 2_000_000:
        return "High"
    return "VIP"


def activity_flag(days: float) -> str:
    if pd.isna(days):
        return "Unknown"
    if days <= 40:
        return "Active"
    elif days <= 90:
        return "Cooling"
    return "Inactive - Reach Out"


def build_investor_summary(df_valid: pd.DataFrame) -> pd.DataFrame:
    phone_agg = {"customer_phone": ("customer_phone", "first")} if "customer_phone" in df_valid.columns else {}

    summary = df_valid.groupby("customer_unique_id").agg(
        customer_name=("customer_name", "first"),
        **phone_agg,
        total_invested=("base_grand_total", "sum"),
        num_investments=("id", "count"),
        avg_investment=("base_grand_total", "mean"),
        first_investment=("invested_created_at", "min"),
        last_investment=("invested_created_at", "max"),
    ).reset_index()

    if "customer_phone" not in summary.columns:
        summary["customer_phone"] = None
    else:
        # Phone column becomes float64 if any investor has a missing number
        # (NaN forces the whole column to float) - strip the resulting
        # trailing ".0" so exports show clean phone numbers.
        summary["customer_phone"] = summary["customer_phone"].apply(
            lambda x: str(int(x)) if pd.notna(x) else None
        )

    summary["tier"] = summary["total_invested"].apply(assign_tier)
    return summary


def build_preference(df_valid: pd.DataFrame) -> pd.DataFrame:
    """Cell 7: favorite product category per investor."""
    pref = (
        df_valid.groupby(["customer_unique_id", "product_category"])["base_grand_total"]
        .sum()
        .unstack(fill_value=0)
    )
    pref["favorite_category"] = pref.idxmax(axis=1)
    return pref[["favorite_category"]].reset_index()


def build_combo_performance(df_valid: pd.DataFrame) -> pd.DataFrame:
    """Cell 8: best performing product + tenure combinations."""
    combo = df_valid.groupby(["product_category", "tenure"]).agg(
        total_raised=("base_grand_total", "sum"),
        num_investors=("customer_unique_id", "nunique"),
        avg_investment=("base_grand_total", "mean"),
    ).sort_values("total_raised", ascending=False).reset_index()
    return combo


def build_final_table(df_valid: pd.DataFrame, investor_summary: pd.DataFrame,
                       preference: pd.DataFrame) -> pd.DataFrame:
    """Cell 9: merges everything into the IR-ready master table, with
    last project, active-investment flag, preferred tenure, days since
    last investment, and activity status."""
    today_ref = pd.Timestamp.now()

    last_project = (
    df_valid.sort_values("invested_created_at")
    .groupby("customer_unique_id")
    .last()[["project_name", "tenure"]]
    .rename(columns={
        "project_name": "last_project_name",
        "tenure": "last_project_tenure",
    })
)

    has_running = (
        df_valid.groupby("customer_unique_id")["status"]
        .apply(lambda x: x.isin(RUNNING_STATUS).any())
        .rename("has_active_investment")
    )

    def most_common_tenure(x: pd.Series):
        """Most frequent tenure for this investor. Ties (equal counts for
        two or more tenure values) are broken deterministically by picking
        the LONGEST tenure among the tied values - plain idxmax() on a tie
        depends on row order, which can silently differ between platforms
        (e.g. Kaggle vs. this web app) and produce a different 'preferred'
        tenure for the exact same investor."""
        counts = x.value_counts()
        max_count = counts.max()
        tied_tenures = counts[counts == max_count].index
        return max(tied_tenures)

    preferred_tenure = (
        df_valid.groupby("customer_unique_id")["tenure"]
        .agg(most_common_tenure)
        .rename("preferred_tenure")
    )

    final = investor_summary.merge(preference, on="customer_unique_id", how="left")
    final = final.merge(last_project, on="customer_unique_id", how="left")
    final = final.merge(has_running, on="customer_unique_id", how="left")
    final = final.merge(preferred_tenure, on="customer_unique_id", how="left")

    last_maturity = (
        df_valid.groupby("customer_unique_id")["maturity_date"]
        .max()
        .rename("last_maturity_date")
    )
    final = final.merge(last_maturity, on="customer_unique_id", how="left")

    final["days_since_last_investment"] = (today_ref - final["last_maturity_date"]).dt.days
    final["activity_status"] = final["days_since_last_investment"].apply(activity_flag)

    final["tier"] = pd.Categorical(final["tier"], categories=TIER_ORDER, ordered=True)
    final = final.sort_values(
        ["tier", "days_since_last_investment"], ascending=[True, False]
    ).reset_index(drop=True)

    return final


def run_pipeline(raw_df: pd.DataFrame, start_date: pd.Timestamp | None,
                 end_date: pd.Timestamp | None = None):
    """End-to-end: raw upload -> (df_valid, final_table, combo_performance)."""
    df_valid = preprocess(raw_df, start_date, end_date)
    investor_summary = build_investor_summary(df_valid)
    preference = build_preference(df_valid)
    combo_performance = build_combo_performance(df_valid)
    final = build_final_table(df_valid, investor_summary, preference)
    return df_valid, final, combo_performance


# --------------------------------------------------------------------------
# Chart builders
# --------------------------------------------------------------------------

def tier_kpi_row(final: pd.DataFrame):
    counts = final["tier"].value_counts().reindex(TIER_ORDER).fillna(0)
    cols = st.columns(4)
    for i, tier in enumerate(TIER_ORDER):
        cols[i].metric(f"{tier} investors", f"{int(counts[tier]):,}")


def tier_bar_chart(final: pd.DataFrame):
    counts = final["tier"].value_counts().reindex(TIER_ORDER).fillna(0)
    fig = px.bar(
        x=counts.index, y=counts.values,
        labels={"x": "Tier", "y": "Number of Investors"},
        title="Investor Tiers",
        color=counts.index,
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig.update_layout(showlegend=False, height=380)
    return fig


def category_bar_chart(df_valid: pd.DataFrame):
    totals = df_valid.groupby("product_category")["base_grand_total"].sum().sort_values(ascending=False)
    fig = px.bar(
        x=totals.index, y=totals.values,
        labels={"x": "Product Category", "y": "Total Invested (৳)"},
        title="Investment by Product Category",
    )
    fig.update_layout(height=420)
    return fig


def tenure_chart(final: pd.DataFrame, tier_filter: str = "All"):
    """Bar chart of preferred_tenure counts, optionally filtered by tier."""
    data = final if tier_filter == "All" else final[final["tier"] == tier_filter]
    counts = data["preferred_tenure"].value_counts().sort_index()
    title = "Investors by Preferred Tenure" + (f" — {tier_filter}" if tier_filter != "All" else " (All Tiers)")
    fig = px.bar(
        x=counts.index.astype(str), y=counts.values,
        labels={"x": "Tenure (months)", "y": "Number of Investors"},
        title=title, text=counts.values,
    )
    # Force a categorical x-axis - otherwise Plotly treats the numeric
    # tenure values (2, 3, 6, 12, 24...) as a continuous scale, spacing
    # bars proportionally to their value and only labeling round numbers
    # (5, 10, 15...). That leaves real values like 2 or 18 unlabeled and
    # stretches large gaps (e.g. between 12 and 24) across the chart.
    # 'category' gives every tenure that actually exists in the data its
    # own evenly-spaced slot and its own visible tick label.
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=counts.index.astype(str))
    fig.update_layout(height=380)
    return fig


def activity_pie(final: pd.DataFrame):
    counts = final["activity_status"].value_counts()
    fig = go.Figure(go.Pie(labels=counts.index, values=counts.values, hole=0.5))
    fig.update_layout(title="Investor Activity Status", height=380)
    return fig


# --------------------------------------------------------------------------
# Streamlit page
# --------------------------------------------------------------------------
def render_export_controls(df: pd.DataFrame, key_prefix: str, filename: str,
                            default_columns: list | None = None):
    """Row-count + column-selection controls before a CSV download.
    Reusable across any table in the dashboard - just give each call
    a unique key_prefix."""
    st.markdown("**Export options**")
    ec1, ec2 = st.columns([1, 2])

    with ec1:
        row_options = ["All"] + list(range(10, 101, 10))
        row_choice = st.selectbox(
            "Rows to export", row_options, index=0, key=f"{key_prefix}_row_limit"
        )

    with ec2:
        export_cols = st.multiselect(
            "Columns to export",
            options=list(df.columns),
            default=default_columns if default_columns is not None else list(df.columns),
            key=f"{key_prefix}_export_cols",
        )

    if not export_cols:
        st.warning("Select at least one column to export.")
        return

    export_df = df[export_cols]
    if row_choice != "All":
        export_df = export_df.head(int(row_choice))

    csv = export_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        f"Download {filename}", csv, filename, "text/csv", key=f"{key_prefix}_download"
    )


def _period_label(start_date, end_date) -> str:
    """Human-readable date range for the PDF subtitle."""
    fmt = "%d %b %Y"
    if start_date is not None and end_date is not None:
        return f" | Investments from {start_date.strftime(fmt)} to {end_date.strftime(fmt)}"
    if start_date is not None:
        return f" | Investments from {start_date.strftime(fmt)} onward"
    if end_date is not None:
        return f" | Investments up to {end_date.strftime(fmt)}"
    return ""


def render():
    st.title("👥 WeGro — Investor Segmentation")
    st.caption("Internal use only. Upload the raw order export to segment investors by tier, product, and activity.")

    uploaded_file = st.file_uploader(
        "Order export file (.xlsx, .xls, or .csv)", type=["xlsx", "xls", "csv"], key="segments_uploader"
    )
    if not uploaded_file:
        st.info("Waiting for a file to be uploaded.")
        return

    try:
        raw_df = load_raw_file(uploaded_file.getvalue(), uploaded_file.name)
    except Exception as e:
        st.error(f"Could not read the uploaded file. Details: {e}")
        return

    missing = validate_columns(raw_df)
    if missing:
        st.error(f"Uploaded file is missing expected columns: {missing}")
        return

    # Date filter controls (start + end = date range)
    st.sidebar.header("Segmentation Filters")
    use_start_date = st.sidebar.checkbox("Filter by start date", value=True)
    start_date = None
    if use_start_date:
        start_date = pd.Timestamp(
            st.sidebar.date_input("Include investments from", value=pd.Timestamp("2024-01-01").date())
        )

    use_end_date = st.sidebar.checkbox("Filter by end date", value=True)
    end_date = None
    if use_end_date:
        # Default to the latest order date in the file, so it starts out as "no cut-off".
        _order_dates = pd.to_datetime(raw_df["order_created_at"], errors="coerce", dayfirst=True)
        _default_end = _order_dates.max()
        _default_end = _default_end.date() if pd.notna(_default_end) else datetime.now().date()
        end_date = pd.Timestamp(
            st.sidebar.date_input(
                "Include investments up to",
                value=_default_end,
                key=f"seg_end_date_{uploaded_file.name}",
            )
        )
        if start_date is not None and end_date < start_date:
            st.sidebar.error("End date must be on or after the start date.")
            return

    with st.spinner("Processing investors..."):
        df_valid, final, combo_performance = run_pipeline(raw_df, start_date, end_date)

    if final.empty:
        st.warning("No valid investments found for the selected filters.")
        return

    st.success(f"{len(final):,} investors segmented from {len(df_valid):,} valid investment rows.")

    # Advanced filter date-range widgets keep stale bounds across reruns
    # because Streamlit only honors `value=` the first time a widget key
    # is created - it ignores it on every rerun after that, even if the
    # underlying `final` data has completely changed (e.g. toggling the
    # start/end-date filter). Detect that the effective date range changed
    # and clear the stored widget state so it re-derives from the new data.
    filter_signature = f"{start_date}|{end_date}"
    if st.session_state.get("_seg_filter_signature") != filter_signature:
        st.session_state.pop("seg_last_range", None)
        st.session_state.pop("seg_first_range", None)
        st.session_state["_seg_filter_signature"] = filter_signature

    # Report figures/KPIs collected as we go, so the same objects can be
    # reused in the PDF export without recalculating anything - same
    # pattern as the Daily Funnel tab.
    report_figures = []

    tier_counts = final["tier"].value_counts().reindex(TIER_ORDER).fillna(0)
    kpi_dict = {f"{tier} investors": f"{int(tier_counts[tier]):,}" for tier in TIER_ORDER}
    kpi_dict["Total investors"] = f"{len(final):,}"
    kpi_dict["Total invested"] = f"Tk {final['total_invested'].sum():,.0f}"

    tier_kpi_row(final)
    st.divider()

    c1, c2 = st.columns(2)
    with c1:
        fig = tier_bar_chart(final)
        st.plotly_chart(fig, use_container_width=True)
        report_figures.append(fig)
    with c2:
        fig = activity_pie(final)
        st.plotly_chart(fig, use_container_width=True)
        report_figures.append(fig)

    fig = category_bar_chart(df_valid)
    st.plotly_chart(fig, use_container_width=True)
    report_figures.append(fig)

    # Tenure chart with tier filter, mirrors the Kaggle/Sheets pattern
    tier_choice = st.selectbox("Preferred tenure — filter by tier", ["All"] + TIER_ORDER)
    tenure_fig = tenure_chart(final, tier_choice)
    st.plotly_chart(tenure_fig, use_container_width=True)
    report_figures.append(tenure_fig)

    st.divider()
    st.subheader("Best Performing Product + Tenure Combinations")
    combo_top20 = combo_performance.head(20)
    st.dataframe(combo_top20, use_container_width=True)
    render_export_controls(combo_performance, key_prefix="combo_export", filename="product_tenure_performance.csv")

    st.divider()
    st.subheader("Investor Master Table")

    c1, c2 = st.columns(2)
    with c1:
        tier_filter_table = st.multiselect("Filter by tier", TIER_ORDER, default=TIER_ORDER, key="seg_tier")
    with c2:
        activity_options = ["Active", "Cooling", "Inactive - Reach Out"]
        status_filter = st.multiselect("Activity status", activity_options, default=activity_options, key="seg_status")

    # Only shown when "Inactive - Reach Out" is part of the selection - lets
    # IR narrow further within that bucket (e.g. 90+ vs 365+ days) without
    # a second, disconnected day-threshold control that could disagree
    # with the status label itself.
    extra_inactive_days = None
    if "Inactive - Reach Out" in status_filter:
        extra_inactive_days = st.number_input(
            "Within 'Inactive - Reach Out': inactive since at least (days)",
            min_value=90, value=90, step=30, key="seg_extra_inactive_days",
            help="90 is the floor for this bucket. Raise it to see only the longest-inactive investors "
                 "(e.g. 365 for a year or more).",
        )

    filtered = final[final["tier"].isin(tier_filter_table)]
    filtered = filtered[filtered["activity_status"].isin(status_filter)]
    if extra_inactive_days and extra_inactive_days > 90:
        # Tighten only the Inactive rows - any Active/Cooling rows that
        # are also selected stay untouched.
        is_inactive = filtered["activity_status"] == "Inactive - Reach Out"
        filtered = filtered[~is_inactive | (filtered["days_since_last_investment"] >= extra_inactive_days)]

    with st.expander("Advanced filters"):
        fc1, fc2, fc3 = st.columns(3)

        with fc1:
            amt_min = float(final["total_invested"].min())
            amt_max = float(final["total_invested"].max())
            amount_range = st.slider(
                "Total invested (Tk)", min_value=amt_min, max_value=amt_max,
                value=(amt_min, amt_max), step=max((amt_max - amt_min) / 100, 1.0),
                key="seg_amount_range",
            )
            min_num_investments = st.number_input(
                "Minimum number of investments", min_value=1, value=1, step=1, key="seg_min_num_investments"
            )

        with fc2:
            category_options = sorted(final["favorite_category"].dropna().unique().tolist())
            category_filter = st.multiselect("Favorite category", category_options, default=[], key="seg_category")
            tenure_options = sorted(final["preferred_tenure"].dropna().unique().tolist())
            tenure_filter = st.multiselect("Preferred tenure (months)", tenure_options, default=[], key="seg_tenure")

        with fc3:
            active_choice = st.selectbox(
                "Currently active investment?", ["All", "Active only", "No active investment"],
                key="seg_active_choice",
            )
            name_search = st.text_input("Search by name (optional)", "", key="seg_name_search")

        date_c1, date_c2 = st.columns(2)
        with date_c1:
            last_range = st.date_input(
                "Last investment between",
                value=(final["last_investment"].min().date(), final["last_investment"].max().date()),
                key="seg_last_range",
            )
        with date_c2:
            first_range = st.date_input(
                "First investment between",
                value=(final["first_investment"].min().date(), final["first_investment"].max().date()),
                key="seg_first_range",
            )

        # Apply advanced filters on top of the tier/inactivity filter above
        filtered = filtered[
            (filtered["total_invested"] >= amount_range[0]) & (filtered["total_invested"] <= amount_range[1])
        ]
        filtered = filtered[filtered["num_investments"] >= min_num_investments]
        if category_filter:
            filtered = filtered[filtered["favorite_category"].isin(category_filter)]
        if tenure_filter:
            filtered = filtered[filtered["preferred_tenure"].isin(tenure_filter)]
        if active_choice == "Active only":
            filtered = filtered[filtered["has_active_investment"] == True]
        elif active_choice == "No active investment":
            filtered = filtered[filtered["has_active_investment"] == False]
        if name_search.strip():
            filtered = filtered[filtered["customer_name"].str.contains(name_search.strip(), case=False, na=False)]
        if isinstance(last_range, tuple) and len(last_range) == 2:
            filtered = filtered[
                (filtered["last_investment"].dt.date >= last_range[0]) & (filtered["last_investment"].dt.date <= last_range[1])
            ]
        if isinstance(first_range, tuple) and len(first_range) == 2:
            filtered = filtered[
                (filtered["first_investment"].dt.date >= first_range[0]) & (filtered["first_investment"].dt.date <= first_range[1])
            ]

    st.caption(f"Showing {len(filtered):,} of {len(final):,} investors.")
    st.dataframe(filtered, use_container_width=True)
    render_export_controls(filtered, key_prefix="seg_export", filename="investor_segments.csv")

    # VIP/High-tier investors who've gone quiet - the single most
    # actionable list for IR, so it's what goes into the PDF's table
    # rather than the full 2,000+ row master table.
    outreach_list = final[
        (final["tier"].isin(["VIP", "High"])) & (final["activity_status"] == "Inactive - Reach Out")
    ][["customer_name", "tier", "total_invested", "favorite_category",
       "last_project_name", "days_since_last_investment"]].head(30)

    st.divider()
    render_pdf_export_section(
        title="WeGro — Investor Segmentation Report",
        subtitle=(
            f"Snapshot as of {datetime.now().strftime('%d %b %Y')}"
            + _period_label(start_date, end_date)
        ),
        kpi_dict=kpi_dict,
        figures=report_figures,
        table_df=outreach_list,
        table_heading="Priority Outreach — VIP/High Tier, Inactive 90+ Days",
        key_prefix="segments",
        file_prefix="ir_investor_segments",
        landscape_table=True,
    )
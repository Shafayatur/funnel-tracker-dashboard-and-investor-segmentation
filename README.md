# Funnel Tracker Dashboard

A Streamlit dashboard that turns a manually-maintained daily tracking spreadsheet into an interactive funnel and trend dashboard. Built for an investor-relations team at an agritech startup to replace a static spreadsheet with something they could actually filter and compare.

## What it does

- Upload an Excel/CSV file — no fixed template required
- Auto-detects the correct header row and matches column-name variants, since the source sheet is edited by hand and headers drift slightly over time
- KPI row: registrations, tickets booked/invested, investment value, conversion rate, new-investor mix
- Funnel chart (Registration → Booking → Investment)
- Daily trend lines, new-vs-old investor mix (stacked bar + donut), cumulative investment value vs. a manually set target, payables tracking
- Three filter modes: date range, specific non-consecutive dates, and side-by-side comparison across periods (daily / monthly / yearly)
- CSV export of the filtered or aggregated view

## A couple of the trickier bits

- **Header/column detection**: rather than assuming a fixed schema, the app scans the first several rows for the best-matching header row and maps known column-name variants to canonical fields, so small formatting drift in the manual sheet doesn't break it.
- **Custom date picker**: Streamlit's built-in calendar widget can lock up when the data spans a year boundary with no month common to both years. A year → month → day dropdown picker built only from dates that actually exist in the data avoids that.
- **Period comparison mode**: aggregates the full dataset by day, month, or year so you can put e.g. August vs. September side by side, not just scroll through one continuous range.

## Stack

Python · Streamlit · Pandas · Plotly

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then upload any daily-tracking spreadsheet that has a "Day" column plus any of the recognized funnel metric columns.

## Auth

The dashboard requires a shared passkey. Configure it through `st.secrets`: use `.streamlit/secrets.toml` for local development, or **Settings → Secrets** in Streamlit Cloud.

This is a single-shared-passkey demo pattern with no per-user audit trail. It should not be used as-is for handling sensitive investor data.

## Note

Built during an AI/prompt-engineering internship for internal investor-relations reporting. This repo is the general-purpose data-processing and dashboarding logic — no company data, spreadsheets, or credentials are included.

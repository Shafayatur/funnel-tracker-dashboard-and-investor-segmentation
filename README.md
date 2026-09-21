# Funnel Tracker Dashboard

A Streamlit dashboard that turns a manually-maintained daily tracking
spreadsheet into an interactive funnel and trend dashboard. Built for an
investor-relations team at an agritech startup to replace a static
spreadsheet with something they could actually filter and compare.

## What it does

- Takes in the daily tracking data either as an uploaded Excel/CSV file or
  a live Google Sheet — no fixed template required either way
- Auto-detects the correct header row and matches column-name variants,
  since the source sheet is edited by hand and headers drift slightly
  over time
- KPI row: registrations, tickets booked/invested, investment value,
  conversion rate, new-investor mix
- Funnel chart (Registration → Booking → Investment), daily trend lines,
  new-vs-old investor mix, cumulative investment value vs. a target,
  payables tracking
- Three filter modes: date range, specific non-consecutive dates, and
  side-by-side comparison across periods (daily / monthly / yearly)

## A couple of the trickier bits

- **Header/column detection**: rather than assuming a fixed schema, the
  app scans the first several rows for the best-matching header row and
  maps known column-name variants to canonical fields, so formatting
  drift in the manual sheet doesn't break it. The live Google Sheets
  source runs through this same detection logic rather than trusting the
  sheet's first row to always be correct.
- **Custom date picker**: Streamlit's built-in calendar widget can lock
  up when the data spans a year boundary with no month common to both
  years. A year → month → day dropdown built only from dates that
  actually exist in the data avoids that.
- **Period comparison mode**: aggregates the full dataset by day, month,
  or year so you can put e.g. August vs. September side by side, not
  just scroll through one continuous range.

## Stack

Python · Streamlit · Pandas · Plotly · gspread

## Note

Built during an AI/prompt-engineering internship for internal
investor-relations reporting. This repo is the general-purpose
data-processing and dashboarding logic — no company data, spreadsheets,
or credentials are included.
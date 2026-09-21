import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials


SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


@st.cache_resource
def get_gspread_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    return gspread.authorize(creds)


def _get_worksheet(sheet_url: str, worksheet_name: str):
    try:
        spreadsheet = get_gspread_client().open_by_url(sheet_url)
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't open Google Sheet at '{sheet_url}'. Details: {exc}"
        ) from exc

    available_names = [worksheet.title for worksheet in spreadsheet.worksheets()]
    if worksheet_name not in available_names:
        raise RuntimeError(
            f"Worksheet '{worksheet_name}' was not found. "
            f"Available worksheets: {available_names}"
        )

    return spreadsheet.worksheet(worksheet_name)


@st.cache_data(ttl=300)
def load_sheet(sheet_url: str, worksheet_name: str) -> pd.DataFrame:
    worksheet = _get_worksheet(sheet_url, worksheet_name)
    return pd.DataFrame(worksheet.get_all_records())


@st.cache_data(ttl=300)
def load_sheet_raw(sheet_url: str, worksheet_name: str) -> pd.DataFrame:
    worksheet = _get_worksheet(sheet_url, worksheet_name)
    return pd.DataFrame(worksheet.get_all_values())
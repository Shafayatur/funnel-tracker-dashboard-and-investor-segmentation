"""
WeGro IR Dashboard
-------------------
Entry point only. Each page owns its own logic in its own module - this
file just configures the app and routes to the selected page's render():

  daily_funnel.py       - Daily Funnel Tracker
  investor_segments.py  - Investor Segmentation
  pdf_utils.py           - PDF export helpers shared by both pages

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""

import streamlit as st

import daily_funnel
import investor_segments

st.set_page_config(
    page_title="WeGro IR Dashboard",
    page_icon="📊",
    layout="wide",
)


def check_passkey():
    if st.session_state.get("authenticated") is True:
        return True

    st.sidebar.title("📊 WeGro IR Dashboard")
    passkey = st.sidebar.text_input("Passkey", type="password")

    if not passkey:
        st.info("Enter the passkey in the sidebar to continue.")
        return False

    if passkey == st.secrets.get("APP_PASSKEY", ""):
        st.session_state["authenticated"] = True
        st.rerun()

    st.sidebar.error("Incorrect passkey.")
    return False


def main():
    if not check_passkey():
        return

    # A sidebar radio, rather than st.tabs(), is important here: st.tabs()
    # runs the code inside every tab on every rerun (tabs only control
    # which main-content area is visually shown), so both pages'
    # st.sidebar.* calls would fire every time and stack on top of each
    # other in the one physical sidebar. A sidebar radio only runs the
    # branch that's actually selected, so only that page's sidebar
    # controls ever appear.
    st.sidebar.title("📊 WeGro IR Dashboard")
    page = st.sidebar.radio(
        "Dashboard", ["📈 Daily Funnel Tracker", "👥 Investor Segmentation"],
        label_visibility="collapsed",
    )
    st.sidebar.divider()

    if page == "📈 Daily Funnel Tracker":
        daily_funnel.render()
    else:
        investor_segments.render()


if __name__ == "__main__":
    main()
import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
from streamlit_autorefresh import st_autorefresh
import subprocess

engine = create_engine(
    "postgresql://loguser:password@localhost:5432/logdb"
)

st.set_page_config(page_title="AIOps Dashboard", layout="wide")

st_autorefresh(interval=5000, key="refresh")

st.title("AIOps Self-Healing Dashboard")

# ------------------------
# Service Health
# ------------------------
st.subheader("Service Health")

services=["frontend","cartservice","redis-cart"]

for svc in services:
    try:
        out = subprocess.check_output(
            ["kubectl","get","pods","-o","jsonpath={.items[*].metadata.name}"]
        ).decode()

        if svc in out:
            st.write(f"{svc}: UP")
        else:
            st.write(f"{svc}: DOWN")

    except:
        st.write(f"{svc}: unknown")


# ------------------------
# Recent Logs
# ------------------------
st.subheader("Recent Logs")

try:
    logs_df = pd.read_sql(
        "SELECT * FROM logs ORDER BY timestamp DESC LIMIT 20;",
        engine
    )
    st.dataframe(logs_df)
except Exception as e:
    st.write("Could not load logs", e)


# ------------------------
# Remediation History
# ------------------------
st.subheader("Remediation History")

try:
    rem_df = pd.read_sql(
        "SELECT * FROM remediation_history ORDER BY timestamp DESC LIMIT 20;",
        engine
    )
    st.dataframe(rem_df)
except Exception as e:
    st.write("Could not load remediation history", e)
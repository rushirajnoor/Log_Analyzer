import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
from streamlit_autorefresh import st_autorefresh
import subprocess
import plotly.express as px

# ---------------------------------
# PAGE CONFIG
# ---------------------------------

st.set_page_config(
    page_title="AIOps Dashboard",
    layout="wide",
    page_icon="🧠"
)

# ---------------------------------
# AUTO REFRESH
# ---------------------------------

st_autorefresh(
    interval=5000,
    key="refresh"
)

# ---------------------------------
# DATABASE
# ---------------------------------

engine = create_engine(
    "postgresql://loguser:password@localhost:5432/logdb"
)

# ---------------------------------
# CUSTOM CSS
# ---------------------------------

st.markdown("""
<style>

.main {
    background-color: #f5f7fb;
}

.block-container {
    padding-top: 1rem;
    padding-bottom: 1rem;
}

.metric-card {
    background: white;
    padding: 20px;
    border-radius: 14px;
    box-shadow: 0px 2px 10px rgba(0,0,0,0.08);
    text-align: center;
}

.section-title {
    font-size: 24px;
    font-weight: 700;
    margin-top: 10px;
    margin-bottom: 15px;
}

</style>
""", unsafe_allow_html=True)

# ---------------------------------
# HEADER
# ---------------------------------

st.title("Intelligent AIOps Dashboard")

st.caption(
    "Autonomous Root Cause Analysis and Self-Healing Infrastructure"
)

# ---------------------------------
# LOAD DATABASE TABLES
# ---------------------------------

try:

    logs_df = pd.read_sql(
        """
        SELECT *
        FROM logs
        ORDER BY timestamp DESC
        LIMIT 100;
        """,
        engine
    )

except:

    logs_df = pd.DataFrame()

try:

    rem_df = pd.read_sql(
        """
        SELECT *
        FROM remediation_history
        ORDER BY timestamp DESC
        LIMIT 100;
        """,
        engine
    )

except:

    rem_df = pd.DataFrame()

# ---------------------------------
# CLUSTER HEALTH
# ---------------------------------

st.markdown(
    '<div class="section-title">Cluster Health</div>',
    unsafe_allow_html=True
)

try:

    raw = subprocess.check_output(
        [
            "kubectl",
            "get",
            "pods",
            "-o",
            "custom-columns="
            "NAME:.metadata.name,"
            "STATUS:.status.phase,"
            "RESTARTS:.status.containerStatuses[0].restartCount"
        ]
    ).decode()

    lines = raw.strip().split("\n")[1:]

    pod_rows = []

    for line in lines:

        parts = line.split()

        if len(parts) >= 3:

            pod_rows.append({
                "Pod": parts[0],
                "Status": parts[1],
                "Restarts": parts[2]
            })

    pod_table = pd.DataFrame(pod_rows)

except Exception as e:

    st.error(f"Could not load cluster data: {e}")

    pod_table = pd.DataFrame()

# ---------------------------------
# KPI METRICS
# ---------------------------------

running = 0
failed = 0

if not pod_table.empty:

    running = len(
        pod_table[
            pod_table["Status"] == "Running"
        ]
    )

    failed = len(
        pod_table[
            pod_table["Status"] != "Running"
        ]
    )

total_logs = len(logs_df)

total_remediations = len(rem_df)

col1, col2, col3, col4 = st.columns(4)

with col1:

    st.metric(
        "Running Pods",
        running
    )

with col2:

    st.metric(
        "Failed Pods",
        failed
    )

with col3:

    st.metric(
        "Recent Logs",
        total_logs
    )

with col4:

    st.metric(
        "Remediations",
        total_remediations
    )

# ---------------------------------
# POD STATUS TABLE
# ---------------------------------

st.markdown(
    '<div class="section-title">Pod Status</div>',
    unsafe_allow_html=True
)

if not pod_table.empty:

    st.dataframe(
        pod_table,
        use_container_width=True,
        height=280
    )

else:

    st.warning("No pod data available")

# ---------------------------------
# RESOURCE METRICS
# ---------------------------------

st.markdown(
    '<div class="section-title">Resource Usage</div>',
    unsafe_allow_html=True
)

try:

    metrics_raw = subprocess.check_output(
        [
            "kubectl",
            "top",
            "pods"
        ]
    ).decode()

    metric_lines = metrics_raw.strip().split("\n")[1:]

    metric_rows = []

    for line in metric_lines:

        parts = line.split()

        if len(parts) >= 3:

            metric_rows.append({
                "Pod": parts[0],
                "CPU": parts[1],
                "Memory": parts[2]
            })

    metrics_df = pd.DataFrame(metric_rows)

    st.dataframe(
        metrics_df,
        use_container_width=True,
        height=280
    )

    # ---------------------------------
    # CPU CHART
    # ---------------------------------

    cpu_chart = px.bar(
        metrics_df,
        x="Pod",
        y="CPU",
        title="CPU Usage Per Pod"
    )

    st.plotly_chart(
        cpu_chart,
        use_container_width=True
    )

except Exception as e:

    st.warning(
        "Metrics server may not be installed"
    )

    st.code(str(e))

# ---------------------------------
# LOG ANALYTICS
# ---------------------------------

st.markdown(
    '<div class="section-title">Log Analytics</div>',
    unsafe_allow_html=True
)

if (
    not logs_df.empty
    and "level" in logs_df.columns
):

    level_counts = (
        logs_df["level"]
        .value_counts()
        .reset_index()
    )

    level_counts.columns = [
        "Level",
        "Count"
    ]

    pie = px.pie(
        level_counts,
        names="Level",
        values="Count",
        hole=0.5,
        title="Log Severity Distribution"
    )

    st.plotly_chart(
        pie,
        use_container_width=True
    )

else:

    st.info("No log analytics available")

# ---------------------------------
# REMEDIATION ANALYTICS
# ---------------------------------

st.markdown(
    '<div class="section-title">Remediation Analytics</div>',
    unsafe_allow_html=True
)

if (
    not rem_df.empty
    and "action" in rem_df.columns
):

    action_counts = (
        rem_df["action"]
        .value_counts()
        .reset_index()
    )

    action_counts.columns = [
        "Action",
        "Count"
    ]

    action_chart = px.bar(
        action_counts,
        x="Action",
        y="Count",
        title="Remediation Actions"
    )

    st.plotly_chart(
        action_chart,
        use_container_width=True
    )

else:

    st.info("No remediation analytics available")

# ---------------------------------
# RECENT LOGS
# ---------------------------------

st.markdown(
    '<div class="section-title">Recent Logs</div>',
    unsafe_allow_html=True
)

if not logs_df.empty:

    st.dataframe(
        logs_df,
        use_container_width=True,
        height=350
    )

else:

    st.warning("No logs available")

# ---------------------------------
# REMEDIATION HISTORY
# ---------------------------------

st.markdown(
    '<div class="section-title">Remediation History</div>',
    unsafe_allow_html=True
)

if not rem_df.empty:

    st.dataframe(
        rem_df,
        use_container_width=True,
        height=350
    )

else:

    st.warning(
        "No remediation history available"
    )

# ---------------------------------
# FOOTER
# ---------------------------------

st.markdown("---")

st.caption(
    "AIOps Autonomous Infrastructure Monitoring Dashboard"
)
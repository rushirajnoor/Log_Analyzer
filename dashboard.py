import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
from streamlit_autorefresh import st_autorefresh
import subprocess
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime


# --------------------------------------------------
# Config
# --------------------------------------------------

st.set_page_config(
    page_title="Log Analyzer",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------------------------------------
# Minimal CSS — only what Streamlit can't do natively
# --------------------------------------------------

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

.stApp { font-family: 'Inter', sans-serif; }
.block-container { padding-top: 1.5rem; padding-bottom: 1rem; }

/* tighter metric cards */
[data-testid="stMetric"] {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 12px 16px;
}

/* tab bar */
.stTabs [data-baseweb="tab-list"] {
    gap: 2rem;
    border-bottom: 1px solid #30363d;
    padding-bottom: 0.5rem;
}
.stTabs [data-baseweb="tab"] {
    font-weight: 500;
    font-size: 1rem;
}
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------
# Auto refresh
# --------------------------------------------------

st_autorefresh(interval=5000, key="auto_refresh")


# --------------------------------------------------
# Database
# --------------------------------------------------

DB_URL = "postgresql://loguser:password@localhost:5432/logdb"
engine = create_engine(DB_URL)

# Chart palette — muted, professional
PALETTE = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#14b8a6"]


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def query(sql):
    """Run a SQL query, return DataFrame. Empty DataFrame on any error."""
    try:
        return pd.read_sql(sql, engine)
    except Exception:
        return pd.DataFrame()


def parse_metric(val, suffix):
    """Parse kubectl metric like '250m' or '128Mi' to int."""
    try:
        return int(str(val).replace(suffix, ""))
    except (ValueError, TypeError):
        return 0


def dark_fig(fig, h=340):
    """Apply dark styling to a plotly figure."""
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter", size=12),
        height=h,
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(bgcolor="rgba(0,0,0,0)", font_size=11),
    )
    fig.update_xaxes(gridcolor="#21262d")
    fig.update_yaxes(gridcolor="#21262d")
    return fig


# --------------------------------------------------
# Load data
# --------------------------------------------------

logs_df = query("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 300")
rem_df = query("SELECT * FROM remediation_history ORDER BY timestamp DESC LIMIT 200")
incidents_df = query("SELECT * FROM incidents ORDER BY created_at DESC LIMIT 200")
eval_df = query("SELECT * FROM evaluation_logs ORDER BY created_at DESC LIMIT 500")

# Pod status
pod_df = pd.DataFrame()
try:
    raw = subprocess.check_output([
        "kubectl", "get", "pods", "-o",
        "custom-columns=NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount"
    ], stderr=subprocess.DEVNULL).decode()
    rows = []
    for line in raw.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) >= 3:
            rows.append({"Pod": parts[0], "Status": parts[1], "Restarts": int(parts[2]) if parts[2].isdigit() else 0})
    pod_df = pd.DataFrame(rows)
except Exception:
    pass

# Resource metrics
res_df = pd.DataFrame()
try:
    raw = subprocess.check_output(["kubectl", "top", "pods"], stderr=subprocess.DEVNULL).decode()
    rows = []
    for line in raw.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) >= 3:
            rows.append({
                "Pod": parts[0],
                "CPU (m)": parse_metric(parts[1], "m"),
                "Memory (Mi)": parse_metric(parts[2], "Mi"),
            })
    res_df = pd.DataFrame(rows)
except Exception:
    pass


# --------------------------------------------------
# Sidebar — filters
# --------------------------------------------------

with st.sidebar:
    st.header("Filters")

    # Service filter
    all_services = []
    if not logs_df.empty and "service" in logs_df.columns:
        all_services = sorted(logs_df["service"].dropna().unique().tolist())

    selected_services = st.multiselect(
        "Services",
        options=all_services,
        default=all_services,
        help="Filter logs and analytics by service"
    )

    st.divider()
    st.caption(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}")
    st.caption("Auto-refreshes every 5s")

# Apply service filter to logs
if selected_services and not logs_df.empty and "service" in logs_df.columns:
    filtered_logs = logs_df[logs_df["service"].isin(selected_services)]
else:
    filtered_logs = logs_df


# --------------------------------------------------
# Compute KPIs
# --------------------------------------------------

running = len(pod_df[pod_df["Status"] == "Running"]) if not pod_df.empty else 0
total_pods = len(pod_df) if not pod_df.empty else 0

active_incidents = 0
if not incidents_df.empty and "status" in incidents_df.columns:
    active_incidents = int((incidents_df["status"] == "active").sum())

avg_mttr = None
if not incidents_df.empty and {"resolved_at", "created_at", "status"}.issubset(incidents_df.columns):
    res = incidents_df[
        (incidents_df["status"] == "resolved") &
        incidents_df["resolved_at"].notna() &
        incidents_df["created_at"].notna()
    ].copy()
    if not res.empty:
        try:
            dur = (pd.to_datetime(res["resolved_at"]) - pd.to_datetime(res["created_at"])).dt.total_seconds()
            avg_mttr = dur.mean()
        except Exception:
            pass

success_rate = None
if not eval_df.empty and "success" in eval_df.columns:
    try:
        n = len(eval_df)
        if n > 0:
            success_rate = float(eval_df["success"].sum()) / n * 100
    except Exception:
        pass

autonomy = None
if not eval_df.empty and "confidence" in eval_df.columns:
    try:
        n = len(eval_df)
        if n > 0:
            autonomy = float((eval_df["confidence"] == "HIGH").sum()) / n * 100
    except Exception:
        pass


# --------------------------------------------------
# Header + KPIs
# --------------------------------------------------

st.title("Log Analyzer")
st.caption("Autonomous root cause analysis and self-healing for Kubernetes microservices")

k1, k2, k3, k4, k5 = st.columns(5)

k1.metric("Pods", f"{running}/{total_pods}", delta=f"{total_pods - running} down" if running < total_pods else "all healthy",
          delta_color="inverse" if running < total_pods else "normal")
k2.metric("Active Incidents", active_incidents)
k3.metric("Avg MTTR", f"{avg_mttr:.1f}s" if avg_mttr is not None else "—")
k4.metric("Success Rate", f"{success_rate:.0f}%" if success_rate is not None else "—")
k5.metric("Autonomy", f"{autonomy:.0f}%" if autonomy is not None else "—")

st.divider()


# --------------------------------------------------
# Tabs
# --------------------------------------------------

tab_overview, tab_analytics, tab_incidents, tab_logs = st.tabs([
    "Overview", "Analytics", "Incidents", "Logs"
])


# ==========================================================================
# TAB: Overview
# ==========================================================================

with tab_overview:

    c1, c2 = st.columns([3, 2])

    # --- Pod table ---
    with c1:
        st.subheader("Pod Status")
        if not pod_df.empty:
            st.dataframe(
                pod_df,
                use_container_width=True,
                height=min(35 * len(pod_df) + 38, 400),
                column_config={
                    "Pod": st.column_config.TextColumn("Pod", width="large"),
                    "Status": st.column_config.TextColumn("Status"),
                    "Restarts": st.column_config.NumberColumn("Restarts", format="%d"),
                },
            )
        else:
            st.info("kubectl not available — no pod data.")

    # --- Quick stats ---
    with c2:
        st.subheader("Recent Activity")

        # error count
        err_count = 0
        warn_count = 0
        if not filtered_logs.empty and "level" in filtered_logs.columns:
            err_count = int((filtered_logs["level"] == "ERROR").sum())
            warn_count = int((filtered_logs["level"] == "WARNING").sum())

        m1, m2 = st.columns(2)
        m1.metric("Errors (recent)", err_count)
        m2.metric("Warnings (recent)", warn_count)

        # Remediations breakdown
        if not rem_df.empty and "verification" in rem_df.columns:
            succ = int((rem_df["verification"] == "success").sum())
            fail = len(rem_df) - succ
            m3, m4 = st.columns(2)
            m3.metric("Remediations OK", succ)
            m4.metric("Remediations Failed", fail)

    # --- Resource charts ---
    if not res_df.empty:
        st.subheader("Resource Usage")
        r1, r2 = st.columns(2)

        with r1:
            fig = px.bar(
                res_df.sort_values("CPU (m)", ascending=True),
                y="Pod", x="CPU (m)", orientation="h",
                color_discrete_sequence=["#3b82f6"],
            )
            fig.update_layout(title_text="CPU (millicores)", showlegend=False)
            dark_fig(fig, h=max(len(res_df) * 32 + 60, 250))
            st.plotly_chart(fig, use_container_width=True)

        with r2:
            fig = px.bar(
                res_df.sort_values("Memory (Mi)", ascending=True),
                y="Pod", x="Memory (Mi)", orientation="h",
                color_discrete_sequence=["#22c55e"],
            )
            fig.update_layout(title_text="Memory (MiB)", showlegend=False)
            dark_fig(fig, h=max(len(res_df) * 32 + 60, 250))
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Metrics server not available. Run `minikube addons enable metrics-server`.")


# ==========================================================================
# TAB: Analytics
# ==========================================================================

with tab_analytics:

    # --- Row 1 ---
    a1, a2 = st.columns(2)

    with a1:
        st.subheader("Log Severity Breakdown")
        if not filtered_logs.empty and "level" in filtered_logs.columns:
            counts = filtered_logs["level"].value_counts().reset_index()
            counts.columns = ["Level", "Count"]
            fig = px.pie(
                counts, names="Level", values="Count", hole=0.45,
                color="Level",
                color_discrete_map={"ERROR": "#ef4444", "WARNING": "#f59e0b", "INFO": "#3b82f6"},
            )
            fig.update_traces(textinfo="percent+label", textfont_size=12)
            dark_fig(fig, 300)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No log data.")

    with a2:
        st.subheader("Errors by Service")
        if not filtered_logs.empty and {"service", "level"}.issubset(filtered_logs.columns):
            errs = filtered_logs[filtered_logs["level"].isin(["ERROR", "WARNING"])]
            if not errs.empty:
                svc = errs.groupby(["service", "level"]).size().reset_index(name="count")
                fig = px.bar(
                    svc, x="service", y="count", color="level", barmode="group",
                    color_discrete_map={"ERROR": "#ef4444", "WARNING": "#f59e0b"},
                    labels={"service": "Service", "count": "Count", "level": "Level"},
                )
                dark_fig(fig, 300)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.success("No errors or warnings in filtered logs.")
        else:
            st.info("No log data.")

    # --- Row 2 (evaluation data) ---
    if not eval_df.empty:

        b1, b2 = st.columns(2)

        with b1:
            st.subheader("Remediation Effectiveness")
            if {"action", "success"}.issubset(eval_df.columns):
                stats = eval_df.groupby("action").agg(
                    total=("success", "count"),
                    wins=("success", "sum"),
                ).reset_index()
                stats["rate"] = (stats["wins"] / stats["total"] * 100).round(1)

                fig = go.Figure()
                fig.add_trace(go.Bar(
                    x=stats["action"], y=stats["rate"],
                    marker_color="#22c55e",
                    text=stats["rate"].apply(lambda v: f"{v:.0f}%"),
                    textposition="outside",
                    name="Success %",
                ))
                fig.update_layout(
                    title_text="Success Rate by Action",
                    yaxis_title="Success %", yaxis_range=[0, 110],
                    showlegend=False,
                )
                dark_fig(fig, 300)
                st.plotly_chart(fig, use_container_width=True)

        with b2:
            st.subheader("Recovery Time")
            if {"action", "recovery_time"}.issubset(eval_df.columns):
                fig = px.box(
                    eval_df, x="action", y="recovery_time",
                    color="action", color_discrete_sequence=PALETTE,
                    labels={"recovery_time": "Seconds", "action": "Action"},
                )
                fig.update_layout(title_text="Recovery Time Distribution", showlegend=False)
                dark_fig(fig, 300)
                st.plotly_chart(fig, use_container_width=True)

        # --- Row 3: Confidence ---
        c1, c2 = st.columns(2)

        with c1:
            st.subheader("Confidence Distribution")
            if "confidence" in eval_df.columns:
                cc = eval_df["confidence"].value_counts().reindex(["HIGH", "MEDIUM", "LOW"]).dropna().reset_index()
                cc.columns = ["Level", "Count"]
                fig = px.bar(
                    cc, x="Level", y="Count",
                    color="Level",
                    color_discrete_map={"HIGH": "#22c55e", "MEDIUM": "#f59e0b", "LOW": "#ef4444"},
                )
                fig.update_layout(title_text="Confidence Levels", showlegend=False)
                dark_fig(fig, 280)
                st.plotly_chart(fig, use_container_width=True)

        with c2:
            st.subheader("Actions per Service")
            if {"service", "action"}.issubset(eval_df.columns):
                sa = eval_df.groupby(["service", "action"]).size().reset_index(name="count")
                fig = px.bar(
                    sa, x="service", y="count", color="action",
                    barmode="stack", color_discrete_sequence=PALETTE,
                    labels={"service": "Service", "count": "Count", "action": "Action"},
                )
                fig.update_layout(title_text="Remediation Actions by Service")
                dark_fig(fig, 280)
                st.plotly_chart(fig, use_container_width=True)

    else:
        st.info("No evaluation data yet — the system logs outcomes after each remediation cycle.")


# ==========================================================================
# TAB: Incidents
# ==========================================================================

with tab_incidents:

    if not incidents_df.empty and "status" in incidents_df.columns:

        active = incidents_df[incidents_df["status"] == "active"]
        resolved = incidents_df[incidents_df["status"] == "resolved"]

        # --- Active incidents ---
        st.subheader(f"Active Incidents ({len(active)})")

        if not active.empty:
            for _, row in active.head(15).iterrows():
                svc = row.get("service", "?")
                cause = str(row.get("cause", ""))[:150]
                fault = row.get("fault_class", "")
                ts = row.get("created_at", "")

                with st.container(border=True):
                    ic1, ic2 = st.columns([4, 1])
                    ic1.markdown(f"**{svc}** — {cause}")
                    ic2.caption(f"{fault}  ·  {ts}")
        else:
            st.success("No active incidents.")

        st.divider()

        # --- Fault breakdown ---
        f1, f2 = st.columns(2)

        with f1:
            st.subheader("Fault Classification")
            if "fault_class" in incidents_df.columns:
                fc = incidents_df["fault_class"].value_counts().reset_index()
                fc.columns = ["Fault", "Count"]
                fig = px.pie(fc, names="Fault", values="Count", hole=0.4, color_discrete_sequence=PALETTE)
                fig.update_traces(textinfo="percent+label", textfont_size=11)
                dark_fig(fig, 280)
                st.plotly_chart(fig, use_container_width=True)

        with f2:
            st.subheader("MTTR by Service")
            if {"resolved_at", "created_at", "service"}.issubset(incidents_df.columns):
                r = resolved[resolved["resolved_at"].notna() & resolved["created_at"].notna()].copy()
                if not r.empty:
                    try:
                        r["mttr"] = (pd.to_datetime(r["resolved_at"]) - pd.to_datetime(r["created_at"])).dt.total_seconds()
                        avg = r.groupby("service")["mttr"].mean().reset_index()
                        avg.columns = ["Service", "Avg MTTR (s)"]
                        avg = avg.sort_values("Avg MTTR (s)", ascending=True)
                        fig = px.bar(
                            avg, y="Service", x="Avg MTTR (s)", orientation="h",
                            color_discrete_sequence=["#3b82f6"],
                        )
                        fig.update_layout(title_text="Avg Recovery Time by Service", showlegend=False)
                        dark_fig(fig, 280)
                        st.plotly_chart(fig, use_container_width=True)
                    except Exception:
                        st.info("Could not compute MTTR.")
                else:
                    st.info("No resolved incidents with timestamps.")

        # --- Resolved table ---
        st.subheader(f"Resolved Incidents ({len(resolved)})")
        if not resolved.empty:
            display_cols = [c for c in ["service", "cause", "fault_class", "created_at", "resolved_at"] if c in resolved.columns]
            st.dataframe(resolved[display_cols].head(50), use_container_width=True, height=300)
        else:
            st.info("No resolved incidents.")

    else:
        st.info("No incident data yet. Incidents appear after the remediation system detects and processes issues.")


# ==========================================================================
# TAB: Logs
# ==========================================================================

with tab_logs:

    l1, l2 = st.columns([3, 2])

    with l1:
        st.subheader("Recent Logs")
        if not filtered_logs.empty:
            display_cols = [c for c in ["timestamp", "level", "service", "message"] if c in filtered_logs.columns]
            st.dataframe(
                filtered_logs[display_cols].head(150),
                use_container_width=True,
                height=500,
                column_config={
                    "timestamp": st.column_config.NumberColumn("Timestamp", format="%.2f"),
                    "level": st.column_config.TextColumn("Level", width="small"),
                    "service": st.column_config.TextColumn("Service", width="medium"),
                    "message": st.column_config.TextColumn("Message", width="large"),
                },
            )
        else:
            st.info("No logs.")

    with l2:
        st.subheader("Remediation History")
        if not rem_df.empty:
            display_cols = [c for c in ["timestamp", "service", "action", "verification"] if c in rem_df.columns]
            st.dataframe(
                rem_df[display_cols].head(100),
                use_container_width=True,
                height=500,
                column_config={
                    "service": st.column_config.TextColumn("Service"),
                    "action": st.column_config.TextColumn("Action"),
                    "verification": st.column_config.TextColumn("Result"),
                },
            )
        else:
            st.info("No remediation history.")
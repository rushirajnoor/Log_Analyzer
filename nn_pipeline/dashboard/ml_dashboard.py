"""
ML Insights Dashboard — Streamlit app for model monitoring & explainability.

Runs on port 8502 (separate from the ops dashboard on 8501).
Launch with: streamlit run nn_pipeline/dashboard/ml_dashboard.py --server.port 8502

4 Tabs:
  1. Real-Time Predictions — Live prediction feed with confidence scores
  2. Attention & Explainability — Heatmaps, GAT edge visualization, saliency
  3. Model Performance — Training curves, confusion matrices, ROC/PR
  4. Embedding Explorer — UMAP/t-SNE of log embeddings
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Lazy imports (dashboard may be run standalone)
# ─────────────────────────────────────────────────────────────────────

def _import_streamlit():
    import streamlit as st
    return st

def _import_plotly():
    import plotly.graph_objects as go
    import plotly.express as px
    return go, px


def main() -> None:
    """Main entry point for the ML Insights Dashboard."""
    st = _import_streamlit()
    go, px = _import_plotly()

    st.set_page_config(
        page_title="🧠 ML Log Analyzer — Insights",
        page_icon="🧠",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ── Dark theme CSS ──
    st.markdown("""
    <style>
    .stApp { background-color: #0e1117; }
    .metric-card {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border-radius: 12px;
        padding: 20px;
        border: 1px solid #30475e;
        margin-bottom: 10px;
    }
    .metric-value {
        font-size: 2.5em;
        font-weight: bold;
        color: #00d4ff;
    }
    .metric-label { color: #888; font-size: 0.9em; }
    .anomaly-high { color: #ff4444; }
    .anomaly-medium { color: #ffaa00; }
    .anomaly-low { color: #44ff44; }
    </style>
    """, unsafe_allow_html=True)

    st.title("🧠 Neural Network Log Analyzer — ML Insights")

    # ── Sidebar ──
    with st.sidebar:
        st.header("⚙️ Configuration")
        model_dir = st.text_input(
            "Checkpoint Directory",
            value=str(Path(__file__).resolve().parent.parent / "checkpoints"),
        )
        st.divider()
        st.caption("Dashboard for monitoring neural network model performance, "
                    "predictions, and explainability visualizations.")

    # ── Tabs ──
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Real-Time Predictions",
        "🔍 Attention & Explainability",
        "📈 Model Performance",
        "🗺️ Embedding Explorer",
    ])

    # ═══════════════════════════════════════════════════════════════
    # Tab 1: Real-Time Predictions
    # ═══════════════════════════════════════════════════════════════
    with tab1:
        st.header("Real-Time Prediction Feed")

        col1, col2, col3, col4 = st.columns(4)
        # Placeholder metrics (populated when connected to live engine)
        with col1:
            st.metric("Anomalies Detected", "—", help="Total anomalies in current session")
        with col2:
            st.metric("Avg Confidence", "—", help="Mean prediction confidence")
        with col3:
            st.metric("Avg Inference (ms)", "—", help="Mean inference time")
        with col4:
            st.metric("Models Loaded", "—", help="Number of loaded models")

        st.divider()

        # Prediction log table
        st.subheader("📋 Recent Predictions")
        st.info(
            "💡 Connect the inference engine to see live predictions here. "
            "Run the pipeline with: `python -m nn_pipeline.training.train_all` "
            "to train models first."
        )

        # Show sample prediction format
        with st.expander("📌 Sample Prediction Format"):
            sample = {
                "is_anomaly": True,
                "anomaly_score": 0.87,
                "severity": "ERROR",
                "fault_type": "network_failure",
                "root_cause_service": "redis-cart",
                "confidence": "HIGH",
                "model_scores": {
                    "transformer": 0.92,
                    "lstm_ae": 0.78,
                    "gat": 0.85,
                    "vae": 0.71,
                },
                "inference_time_ms": 4.2,
            }
            st.json(sample)

        # Severity distribution over time (placeholder chart)
        st.subheader("📊 Severity Distribution")
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=["INFO", "WARNING", "ERROR"],
            y=[85, 10, 5],
            marker_color=["#44ff44", "#ffaa00", "#ff4444"],
        ))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            title="Log Level Distribution (sample)",
            yaxis_title="Percentage",
            height=350,
        )
        st.plotly_chart(fig, use_container_width=True)

    # ═══════════════════════════════════════════════════════════════
    # Tab 2: Attention & Explainability
    # ═══════════════════════════════════════════════════════════════
    with tab2:
        st.header("Attention & Explainability Visualizations")

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("🔥 Transformer Attention Heatmap")
            # Generate sample attention heatmap
            np.random.seed(42)
            sample_attn = np.random.rand(16, 16)
            sample_attn = sample_attn / sample_attn.sum(axis=1, keepdims=True)

            fig = go.Figure(data=go.Heatmap(
                z=sample_attn,
                colorscale="Viridis",
                colorbar_title="Weight",
            ))
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                title="Self-Attention Weights (sample, last layer)",
                xaxis_title="Key Position",
                yaxis_title="Query Position",
                height=400,
            )
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.subheader("🔗 GAT Service Dependency Graph")
            st.info(
                "Service dependency attention weights will be displayed here "
                "after training. Each edge shows how strongly the model "
                "believes a dependency contributed to the failure."
            )

            # Sample service graph as adjacency
            services = ["frontend", "cart", "checkout", "payment",
                         "redis", "product", "recommend", "currency"]
            sample_adj = np.random.rand(8, 8) * 0.3
            np.fill_diagonal(sample_adj, 0)

            fig = go.Figure(data=go.Heatmap(
                z=sample_adj,
                x=services,
                y=services,
                colorscale="Hot",
                colorbar_title="Attention",
            ))
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                title="Service-to-Service Attention (sample)",
                height=400,
            )
            st.plotly_chart(fig, use_container_width=True)

        st.divider()

        st.subheader("📊 Feature Importance (Saliency)")
        feature_names = [
            "severity", "service", "time_sin", "time_cos", "time_delta",
            "msg_length", "token_count", "template_id", "is_error",
            "is_warning", "has_error_field", "kw_error", "kw_fail",
            "kw_timeout", "kw_refused",
        ]
        np.random.seed(42)
        sample_importance = np.random.exponential(0.3, len(feature_names))
        sample_importance = sample_importance / sample_importance.max()

        sorted_idx = np.argsort(sample_importance)
        fig = go.Figure(go.Bar(
            x=sample_importance[sorted_idx],
            y=[feature_names[i] for i in sorted_idx],
            orientation='h',
            marker_color='#00d4ff',
        ))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            title="Feature Importance via Gradient Saliency (sample)",
            xaxis_title="Importance",
            height=450,
        )
        st.plotly_chart(fig, use_container_width=True)

    # ═══════════════════════════════════════════════════════════════
    # Tab 3: Model Performance
    # ═══════════════════════════════════════════════════════════════
    with tab3:
        st.header("Model Performance Metrics")

        # Check for evaluation results
        results_path = Path(model_dir) / "evaluation_results.json"
        if results_path.exists():
            with open(results_path) as f:
                results = json.load(f)
            st.success("✅ Evaluation results loaded")
            st.json(results)
        else:
            st.warning(
                "⚠️ No evaluation results found. Train models first with:\n"
                "`python -m nn_pipeline.training.train_all`"
            )

        st.divider()

        # Model comparison table
        st.subheader("📊 Model Comparison")
        col1, col2 = st.columns(2)

        with col1:
            models = ["Transformer", "LSTM-AE", "GAT", "VAE", "Distilled GRU"]
            params = ["~2M", "~500K", "~300K", "~100K", "~200K"]
            latency = ["~15ms", "~8ms", "~12ms", "~3ms", "~2ms"]
            status = ["⏳", "⏳", "⏳", "⏳", "⏳"]

            # Check which models are trained
            ckpt_dir = Path(model_dir)
            ckpt_names = [
                "transformer_best.pt", "lstm_ae_best.pt",
                "gat_best.pt", "vae_best.pt", "distilled_best.pt",
            ]
            for i, name in enumerate(ckpt_names):
                if (ckpt_dir / name).exists():
                    status[i] = "✅"

            import pandas as pd
            df = pd.DataFrame({
                "Model": models,
                "Parameters": params,
                "Latency": latency,
                "Status": status,
            })
            st.dataframe(df, use_container_width=True, hide_index=True)

        with col2:
            # Sample training curve
            epochs = list(range(1, 51))
            np.random.seed(42)
            train_loss = [2.0 * np.exp(-0.05 * e) + 0.1 + np.random.normal(0, 0.02) for e in epochs]
            val_loss = [2.2 * np.exp(-0.04 * e) + 0.15 + np.random.normal(0, 0.03) for e in epochs]

            fig = go.Figure()
            fig.add_trace(go.Scatter(x=epochs, y=train_loss, name="Train Loss",
                                     line=dict(color="#00d4ff")))
            fig.add_trace(go.Scatter(x=epochs, y=val_loss, name="Val Loss",
                                     line=dict(color="#ff6b6b")))
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                title="Training Curves (sample)",
                xaxis_title="Epoch",
                yaxis_title="Loss",
                height=350,
            )
            st.plotly_chart(fig, use_container_width=True)

        st.divider()

        # Confusion matrix (sample)
        st.subheader("📉 Confusion Matrix")
        sample_cm = np.array([[950, 15, 5], [10, 45, 5], [3, 2, 65]])
        labels = ["INFO", "WARNING", "ERROR"]

        fig = go.Figure(data=go.Heatmap(
            z=sample_cm,
            x=labels,
            y=labels,
            text=sample_cm,
            texttemplate="%{text}",
            colorscale="Blues",
        ))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            title="Severity Classification Confusion Matrix (sample)",
            xaxis_title="Predicted",
            yaxis_title="Actual",
            height=400,
        )
        st.plotly_chart(fig, use_container_width=True)

    # ═══════════════════════════════════════════════════════════════
    # Tab 4: Embedding Explorer
    # ═══════════════════════════════════════════════════════════════
    with tab4:
        st.header("Log Embedding Explorer")

        st.info(
            "This tab visualizes log embeddings in 2D using UMAP dimensionality "
            "reduction. After training the Log2Vec contrastive model, embeddings "
            "will be projected and displayed here.\n\n"
            "Expected behavior: similar log messages cluster together, "
            "and error patterns form distinct clusters away from normal logs."
        )

        # Sample UMAP visualization
        np.random.seed(42)
        n_points = 500

        # Generate clustered points
        clusters = {
            "INFO (normal)": (np.random.randn(300, 2) * 0.5 + [0, 0], "#44ff44"),
            "WARNING": (np.random.randn(50, 2) * 0.3 + [3, 2], "#ffaa00"),
            "ERROR (network)": (np.random.randn(30, 2) * 0.2 + [-2, 3], "#ff4444"),
            "ERROR (service)": (np.random.randn(20, 2) * 0.2 + [4, -2], "#ff6666"),
            "ERROR (resource)": (np.random.randn(10, 2) * 0.15 + [-3, -3], "#ff8888"),
        }

        fig = go.Figure()
        for label, (points, color) in clusters.items():
            fig.add_trace(go.Scatter(
                x=points[:, 0],
                y=points[:, 1],
                mode="markers",
                name=label,
                marker=dict(size=5, color=color, opacity=0.7),
            ))

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            title="UMAP Projection of Log Embeddings (sample)",
            xaxis_title="UMAP-1",
            yaxis_title="UMAP-2",
            height=600,
            legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99),
        )
        st.plotly_chart(fig, use_container_width=True)

        st.divider()

        st.subheader("📊 Template Distribution")
        # Sample template distribution
        template_names = [
            "received ad request",
            "order confirmation email",
            "conversion request successful",
            "Getting supported currencies",
            "failed to retrieve ads",
            "request error",
            "connection refused",
            "Starting gRPC server",
            "Other templates",
        ]
        counts = [5000, 3000, 2000, 1500, 500, 143, 31, 200, 1000]

        fig = go.Figure(go.Bar(
            x=counts,
            y=template_names,
            orientation='h',
            marker_color='#00d4ff',
        ))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            title="Log Template Distribution (sample)",
            xaxis_title="Count",
            height=400,
        )
        st.plotly_chart(fig, use_container_width=True)


if __name__ == "__main__":
    main()

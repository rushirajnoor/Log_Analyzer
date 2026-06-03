"""
Explainability module for neural network predictions.

Provides tools to understand WHY the model made a decision:
  - Attention weight extraction and formatting from Transformer
  - GAT edge attention visualization data
  - Gradient-based feature importance (saliency maps)
  - Human-readable explanation generation
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F

from nn_pipeline.config import (
    ERROR_KEYWORDS,
    IDX_TO_FAULT,
    IDX_TO_LEVEL,
    IDX_TO_SERVICE,
    SERVICES,
    SERVICE_DEPENDENCIES,
)

logger = logging.getLogger(__name__)


@dataclass
class ExplainedPrediction:
    """A prediction with full explainability data attached.

    Attributes:
        top_attended_entries: Indices of log entries the Transformer attended to most.
        attention_heatmap: 2D attention matrix (seq_len × seq_len) for the last layer.
        feature_importance: Per-feature importance scores for the prediction.
        feature_names: Names corresponding to feature_importance indices.
        gat_edge_weights: Edge attention weights showing dependency activations.
        dependency_path: The inferred failure propagation path through services.
        explanation_text: Multi-line human-readable explanation.
    """
    top_attended_entries: List[int] = field(default_factory=list)
    attention_heatmap: Optional[np.ndarray] = None
    feature_importance: Optional[np.ndarray] = None
    feature_names: List[str] = field(default_factory=list)
    gat_edge_weights: Optional[Dict[str, float]] = None
    dependency_path: List[str] = field(default_factory=list)
    explanation_text: str = ""


# ─────────────────────────────────────────────────────────────────────
# Attention extraction
# ─────────────────────────────────────────────────────────────────────


def extract_transformer_attention(
    attention_weights: Dict[str, torch.Tensor],
    top_k: int = 10,
) -> Tuple[np.ndarray, List[int]]:
    """Extract and format Transformer attention weights.

    Args:
        attention_weights: Dict from model output with layer attention tensors.
        top_k: Number of top-attended positions to return.

    Returns:
        Tuple of (attention_heatmap as numpy, list of top-k attended indices).
    """
    if not attention_weights:
        return np.zeros((1, 1)), []

    # Use last layer's attention, average across heads
    last_layer_key = sorted(attention_weights.keys())[-1]
    attn = attention_weights[last_layer_key]  # (num_heads, seq_len, seq_len) or similar

    if isinstance(attn, torch.Tensor):
        attn = attn.detach().cpu().numpy()

    if attn.ndim == 3:
        # Average across heads
        attn_avg = attn.mean(axis=0)  # (seq_len, seq_len)
    elif attn.ndim == 2:
        attn_avg = attn
    else:
        return np.zeros((1, 1)), []

    # Get top-k attended positions (sum attention received by each position)
    attention_received = attn_avg.sum(axis=0)  # (seq_len,)
    top_indices = np.argsort(attention_received)[-top_k:][::-1].tolist()

    return attn_avg, top_indices


def extract_gat_edge_attention(
    attention_weights: Dict,
    edge_index: Optional[torch.Tensor] = None,
) -> Dict[str, float]:
    """Extract GAT edge attention weights as service→service mapping.

    Returns:
        Dict mapping "service_a → service_b" → attention_weight.
    """
    result: Dict[str, float] = {}

    if not attention_weights or edge_index is None:
        return result

    # Use last layer attention
    last_key = sorted(attention_weights.keys())[-1]
    edge_attn = attention_weights[last_key]

    if isinstance(edge_attn, torch.Tensor):
        edge_attn = edge_attn.detach().cpu().numpy()
    elif isinstance(edge_attn, list):
        edge_attn = np.array(edge_attn)

    if isinstance(edge_index, torch.Tensor):
        edge_index = edge_index.detach().cpu().numpy()

    src_nodes = edge_index[0]
    tgt_nodes = edge_index[1]

    for i in range(min(len(src_nodes), len(edge_attn))):
        src_svc = IDX_TO_SERVICE.get(int(src_nodes[i]), f"svc_{src_nodes[i]}")
        tgt_svc = IDX_TO_SERVICE.get(int(tgt_nodes[i]), f"svc_{tgt_nodes[i]}")
        key = f"{src_svc} → {tgt_svc}"
        result[key] = float(edge_attn[i])

    return result


# ─────────────────────────────────────────────────────────────────────
# Gradient-based feature importance (saliency)
# ─────────────────────────────────────────────────────────────────────


def compute_saliency(
    model: torch.nn.Module,
    input_features: torch.Tensor,
    target_class: int,
    output_key: str = "severity_logits",
) -> np.ndarray:
    """Compute gradient-based saliency map for input features.

    Args:
        model: The model to explain.
        input_features: Input tensor with requires_grad=True.
        target_class: Index of the target class to compute gradients for.
        output_key: Which output head to use for gradients.

    Returns:
        Saliency map as numpy array, same shape as input_features.
    """
    model.eval()
    input_features = input_features.clone().detach().requires_grad_(True)

    output = model(input_features)
    logits = output[output_key]

    if logits.dim() == 2:
        score = logits[0, target_class]
    else:
        score = logits[target_class]

    score.backward()

    saliency = input_features.grad.detach().abs().cpu().numpy()
    return saliency


# ─────────────────────────────────────────────────────────────────────
# Feature importance names
# ─────────────────────────────────────────────────────────────────────

FEATURE_NAMES = [
    "severity_encoded",
    "service_idx",
    "time_of_day_sin",
    "time_of_day_cos",
    "time_delta",
    "message_length",
    "token_count",
    "template_id",
    "is_error",
    "is_warning",
    "has_error_field",
] + [f"kw_{kw}" for kw in ERROR_KEYWORDS]


# ─────────────────────────────────────────────────────────────────────
# Dependency path tracing
# ─────────────────────────────────────────────────────────────────────


def trace_failure_path(
    root_service: str,
    gat_edge_weights: Dict[str, float],
    threshold: float = 0.3,
) -> List[str]:
    """Trace the failure propagation path from root cause through dependencies.

    Args:
        root_service: The predicted root cause service.
        gat_edge_weights: Edge attention weights from GAT.
        threshold: Minimum edge weight to include in path.

    Returns:
        Ordered list of services in the failure propagation path.
    """
    path = [root_service]
    visited = {root_service}

    # BFS through high-attention edges
    current = root_service
    for _ in range(len(SERVICES)):
        best_next = None
        best_weight = threshold

        for edge_key, weight in gat_edge_weights.items():
            parts = edge_key.split(" → ")
            if len(parts) != 2:
                continue
            src, tgt = parts

            if src == current and tgt not in visited and weight > best_weight:
                best_next = tgt
                best_weight = weight

        if best_next is None:
            break

        path.append(best_next)
        visited.add(best_next)
        current = best_next

    return path


# ─────────────────────────────────────────────────────────────────────
# Human-readable explanation generator
# ─────────────────────────────────────────────────────────────────────


def generate_explanation(
    prediction,  # Prediction dataclass
    top_features: Optional[List[Tuple[str, float]]] = None,
    failure_path: Optional[List[str]] = None,
) -> str:
    """Generate a detailed human-readable explanation for a prediction.

    Args:
        prediction: The Prediction dataclass from inference engine.
        top_features: Top contributing features as (name, importance) pairs.
        failure_path: Service failure propagation path.

    Returns:
        Multi-line explanation string.
    """
    lines = []
    lines.append("=" * 60)
    lines.append("  NEURAL NETWORK LOG ANALYSIS REPORT")
    lines.append("=" * 60)

    # Status
    if prediction.is_anomaly:
        lines.append(f"\n🚨 ANOMALY DETECTED (score: {prediction.anomaly_score:.3f})")
        lines.append(f"   Confidence: {prediction.confidence}")
        lines.append(f"   Severity: {prediction.severity} (score: {prediction.severity_score:.3f})")
        lines.append(f"   Fault Type: {prediction.fault_type}")
        lines.append(f"   Root Cause: {prediction.root_cause_service} "
                      f"(confidence: {prediction.root_cause_confidence:.3f})")
    else:
        lines.append("\n✅ SYSTEM NORMAL")
        lines.append(f"   Anomaly score: {prediction.anomaly_score:.3f}")

    # Model breakdown
    lines.append("\n📊 Model Scores:")
    for model_name, score in prediction.model_scores.items():
        lines.append(f"   • {model_name}: {score:.4f}")

    # Feature importance
    if top_features:
        lines.append("\n🔍 Top Contributing Features:")
        for feat_name, importance in top_features[:7]:
            bar = "█" * int(importance * 20)
            lines.append(f"   • {feat_name:25s} {importance:.4f} {bar}")

    # Failure path
    if failure_path and len(failure_path) > 1:
        lines.append("\n🔗 Failure Propagation Path:")
        path_str = " → ".join(failure_path)
        lines.append(f"   {path_str}")

    # Inference time
    lines.append(f"\n⏱  Inference time: {prediction.inference_time_ms:.2f}ms")
    lines.append("=" * 60)

    return "\n".join(lines)


def explain_prediction(
    prediction,  # Prediction dataclass
    model=None,
    input_features: Optional[torch.Tensor] = None,
) -> ExplainedPrediction:
    """Full explainability pipeline for a prediction.

    Combines attention extraction, saliency computation, dependency
    tracing, and explanation generation into a single call.

    Returns:
        ExplainedPrediction with all explainability data.
    """
    explained = ExplainedPrediction()

    # Attention heatmap
    if prediction.attention_data and "transformer" in prediction.attention_data:
        heatmap, top_idx = extract_transformer_attention(
            prediction.attention_data["transformer"]
        )
        explained.attention_heatmap = heatmap
        explained.top_attended_entries = top_idx

    # GAT edge weights
    if prediction.attention_data and "gat" in prediction.attention_data:
        gat_data = prediction.attention_data["gat"]
        if "edge_attention" in gat_data:
            explained.gat_edge_weights = gat_data["edge_attention"]

    # Saliency
    if model is not None and input_features is not None:
        try:
            saliency = compute_saliency(model, input_features, target_class=0)
            # Average saliency across sequence
            if saliency.ndim == 3:
                feat_importance = saliency.squeeze(0).mean(axis=0)
            elif saliency.ndim == 2:
                feat_importance = saliency.mean(axis=0)
            else:
                feat_importance = saliency

            explained.feature_importance = feat_importance
            explained.feature_names = FEATURE_NAMES[:len(feat_importance)]
        except Exception as e:
            logger.warning("Saliency computation failed: %s", e)

    # Dependency path
    if explained.gat_edge_weights and prediction.root_cause_service != "unknown":
        explained.dependency_path = trace_failure_path(
            prediction.root_cause_service,
            explained.gat_edge_weights,
        )

    # Generate text explanation
    top_feats = None
    if explained.feature_importance is not None:
        names = explained.feature_names
        importances = explained.feature_importance
        pairs = sorted(zip(names, importances), key=lambda x: x[1], reverse=True)
        top_feats = pairs[:10]

    explained.explanation_text = generate_explanation(
        prediction,
        top_features=top_feats,
        failure_path=explained.dependency_path,
    )

    return explained

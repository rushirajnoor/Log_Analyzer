"""
Graph Attention Network (GAT) for Root Cause Analysis.

Models the Kubernetes service dependency graph as a graph where:
  - **Nodes** = services (12 nodes), each with aggregated log features.
  - **Edges** = service dependencies (known + learned).
  - **Task** = Predict root-cause service, failure propagation, and risk.

Architecture:
  3 GAT layers with multi-head attention → graph-level readout →
  3 prediction heads (root cause, propagation, risk score).

Uses PyTorch Geometric (PyG) for efficient graph operations, with
a fallback pure-PyTorch implementation when PyG is not installed.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from nn_pipeline.config import (
    gat_config as cfg,
    SERVICE_DEPENDENCIES,
    SERVICES,
    SERVICE_TO_IDX,
    NUM_SERVICES,
)

# ─────────────────────────────────────────────────────────────────────
# Try importing PyTorch Geometric; fall back to pure PyTorch
# ─────────────────────────────────────────────────────────────────────

_HAS_PYG = False
try:
    from torch_geometric.nn import GATConv
    from torch_geometric.data import Data as PyGData
    _HAS_PYG = True
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────────────
# Build static edge index from known dependencies
# ─────────────────────────────────────────────────────────────────────


def build_dependency_edge_index() -> torch.Tensor:
    """Build a directed edge index from the hardcoded service dependency graph.

    Each edge goes from dependent → dependency (parent → child).

    Returns:
        Tensor of shape ``(2, num_edges)`` with source and target node indices.
    """
    src_list: List[int] = []
    tgt_list: List[int] = []

    for parent, children in SERVICE_DEPENDENCIES.items():
        if parent not in SERVICE_TO_IDX:
            continue
        parent_idx = SERVICE_TO_IDX[parent]
        for child in children:
            if child not in SERVICE_TO_IDX:
                continue
            child_idx = SERVICE_TO_IDX[child]
            src_list.append(parent_idx)
            tgt_list.append(child_idx)
            # Add reverse edge for message passing in both directions
            src_list.append(child_idx)
            tgt_list.append(parent_idx)

    return torch.tensor([src_list, tgt_list], dtype=torch.long)


# ─────────────────────────────────────────────────────────────────────
# Pure PyTorch GAT layer (fallback when PyG unavailable)
# ─────────────────────────────────────────────────────────────────────


class GATLayer(nn.Module):
    """Single-head Graph Attention layer (pure PyTorch implementation).

    Implements the attention mechanism from Veličković et al. (2018):
        attention(i,j) = LeakyReLU(a^T [Wh_i || Wh_j])
    """

    def __init__(
        self,
        in_features: int,
        out_features: int,
        dropout: float = 0.1,
        alpha: float = 0.2,
        concat: bool = True,
    ) -> None:
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.concat = concat

        self.W = nn.Linear(in_features, out_features, bias=False)
        self.a = nn.Parameter(torch.empty(2 * out_features, 1))
        nn.init.xavier_uniform_(self.a.data)

        self.leaky_relu = nn.LeakyReLU(alpha)
        self.dropout = nn.Dropout(dropout)

    def forward(
        self,
        h: torch.Tensor,           # (num_nodes, in_features)
        edge_index: torch.Tensor,  # (2, num_edges)
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Forward pass.

        Returns:
            Tuple of (updated node features, attention weights per edge).
        """
        num_nodes = h.size(0)
        Wh = self.W(h)  # (num_nodes, out_features)

        src, tgt = edge_index  # (num_edges,) each

        # Compute attention coefficients
        Wh_src = Wh[src]  # (num_edges, out_features)
        Wh_tgt = Wh[tgt]  # (num_edges, out_features)
        e = self.leaky_relu(
            torch.cat([Wh_src, Wh_tgt], dim=-1) @ self.a
        ).squeeze(-1)  # (num_edges,)

        # Softmax per target node
        attention = torch.full((num_nodes, num_nodes), float('-inf'), device=h.device)
        attention[tgt, src] = e
        attention = F.softmax(attention, dim=-1)
        attention = self.dropout(attention)

        # Aggregate
        h_prime = torch.matmul(attention, Wh)  # (num_nodes, out_features)

        # Extract per-edge attention weights for explainability
        edge_attention = attention[tgt, src]  # (num_edges,)

        if self.concat:
            return F.elu(h_prime), edge_attention
        else:
            return h_prime, edge_attention


class MultiHeadGATLayer(nn.Module):
    """Multi-head GAT layer (pure PyTorch)."""

    def __init__(
        self,
        in_features: int,
        out_features: int,
        num_heads: int,
        dropout: float = 0.1,
        concat: bool = True,
    ) -> None:
        super().__init__()
        self.heads = nn.ModuleList([
            GATLayer(in_features, out_features, dropout=dropout, concat=concat)
            for _ in range(num_heads)
        ])
        self.concat = concat
        self.num_heads = num_heads

    def forward(
        self,
        h: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        head_outputs = []
        head_attns = []
        for head in self.heads:
            out, attn = head(h, edge_index)
            head_outputs.append(out)
            head_attns.append(attn)

        if self.concat:
            h_out = torch.cat(head_outputs, dim=-1)  # (num_nodes, out_features * num_heads)
        else:
            h_out = torch.mean(torch.stack(head_outputs), dim=0)

        attn_out = torch.mean(torch.stack(head_attns), dim=0)  # (num_edges,)
        return h_out, attn_out


# ─────────────────────────────────────────────────────────────────────
# Edge Predictor (learns additional edges)
# ─────────────────────────────────────────────────────────────────────


class EdgePredictor(nn.Module):
    """Predicts edge probabilities between all service pairs.

    Used to discover latent dependencies not in the hardcoded graph.
    """

    def __init__(self, node_dim: int, hidden_dim: int = 32) -> None:
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(2 * node_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1),
            nn.Sigmoid(),
        )

    def forward(self, node_features: torch.Tensor) -> torch.Tensor:
        """Predict edge probabilities for all node pairs.

        Args:
            node_features: Shape ``(num_nodes, node_dim)``.

        Returns:
            Edge probability matrix, shape ``(num_nodes, num_nodes)``.
        """
        n = node_features.size(0)
        # Create all pairs
        row = node_features.unsqueeze(1).expand(-1, n, -1)  # (n, n, dim)
        col = node_features.unsqueeze(0).expand(n, -1, -1)  # (n, n, dim)
        pairs = torch.cat([row, col], dim=-1)  # (n, n, 2*dim)
        probs = self.mlp(pairs).squeeze(-1)  # (n, n)
        return probs


# ─────────────────────────────────────────────────────────────────────
# Main GAT-RCA Model
# ─────────────────────────────────────────────────────────────────────


class GATRCA(nn.Module):
    """Graph Attention Network for Root Cause Analysis.

    Architecture:
        Node features (128-dim per service)
        → GAT Layer 1 (4 heads × 64 = 256)
        → GAT Layer 2 (4 heads × 64 = 256)
        → GAT Layer 3 (2 heads × 32 = 64, no concat → avg)
        → Graph readout (attention-weighted pooling)
        → 3 prediction heads:
            1. Root cause service (12-class classification)
            2. Failure propagation (edge activation probabilities)
            3. Cascading failure risk score (regression)

    The model can optionally learn additional edges beyond the
    known dependency graph using the ``EdgePredictor``.
    """

    def __init__(self, config=None) -> None:
        super().__init__()
        c = config or cfg

        self.num_services = c.num_services
        self.learn_edges = c.learn_edges

        # Node feature projection
        self.node_proj = nn.Linear(c.node_feature_dim, c.node_feature_dim)

        # GAT layers
        self.gat1 = MultiHeadGATLayer(
            c.node_feature_dim, c.hidden_dim, c.num_heads_layer1,
            dropout=c.dropout, concat=True,
        )
        gat1_out = c.hidden_dim * c.num_heads_layer1

        self.gat2 = MultiHeadGATLayer(
            gat1_out, c.hidden_dim, c.num_heads_layer2,
            dropout=c.dropout, concat=True,
        )
        gat2_out = c.hidden_dim * c.num_heads_layer2

        self.gat3 = MultiHeadGATLayer(
            gat2_out, c.output_dim, c.num_heads_layer3,
            dropout=c.dropout, concat=False,  # Average heads
        )

        # Graph-level readout (attention-weighted)
        self.readout_attn = nn.Linear(c.output_dim, 1)

        # Prediction heads
        self.root_cause_head = nn.Sequential(
            nn.Linear(c.output_dim, c.hidden_dim),
            nn.ReLU(),
            nn.Dropout(c.dropout),
            nn.Linear(c.hidden_dim, c.num_services),
        )

        self.propagation_head = nn.Sequential(
            nn.Linear(c.output_dim * 2, c.hidden_dim),
            nn.ReLU(),
            nn.Linear(c.hidden_dim, 1),
            nn.Sigmoid(),
        )

        self.risk_head = nn.Sequential(
            nn.Linear(c.output_dim, c.hidden_dim),
            nn.ReLU(),
            nn.Dropout(c.dropout),
            nn.Linear(c.hidden_dim, 1),
            nn.Sigmoid(),
        )

        # Edge predictor (optional)
        if self.learn_edges:
            self.edge_predictor = EdgePredictor(c.node_feature_dim)

        # Static edges
        self.register_buffer(
            "static_edge_index",
            build_dependency_edge_index(),
        )

        # Layer norms
        self.norm1 = nn.LayerNorm(gat1_out)
        self.norm2 = nn.LayerNorm(gat2_out)
        self.norm3 = nn.LayerNorm(c.output_dim)

    def _get_edge_index(
        self,
        node_features: torch.Tensor,
        threshold: float = 0.5,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        """Get edge index, optionally augmented with learned edges.

        Returns:
            Tuple of (edge_index, learned_edge_probs or None).
        """
        static_edges = self.static_edge_index

        if not self.learn_edges:
            return static_edges, None

        # Predict additional edges
        edge_probs = self.edge_predictor(node_features)  # (n, n)

        # Mask out self-loops and existing static edges
        n = node_features.size(0)
        mask = torch.ones(n, n, device=node_features.device, dtype=torch.bool)
        mask.fill_diagonal_(False)
        static_src, static_tgt = static_edges
        mask[static_src, static_tgt] = False

        # Get new edges above threshold
        new_edges = (edge_probs * mask.float() > threshold).nonzero(as_tuple=False)

        if new_edges.size(0) > 0:
            new_edge_index = new_edges.t()  # (2, num_new_edges)
            combined = torch.cat([static_edges, new_edge_index], dim=1)
        else:
            combined = static_edges

        return combined, edge_probs

    def forward(
        self,
        node_features: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Forward pass.

        Args:
            node_features: Per-service aggregated features.
                Shape ``(num_services, node_feature_dim)`` for single graph,
                or ``(batch, num_services, node_feature_dim)`` for batched.

        Returns:
            Dict with keys:
              - ``root_cause_logits``: Shape ``(num_services,)`` or ``(batch, num_services)``.
              - ``propagation_probs``: Edge activation probabilities.
              - ``risk_score``: Scalar cascading failure risk.
              - ``attention_weights``: Dict of attention weights per layer.
              - ``learned_edge_probs``: Edge prediction probabilities (if learn_edges).
        """
        # Handle batched input
        if node_features.dim() == 3:
            batch_results = []
            for i in range(node_features.size(0)):
                batch_results.append(self.forward(node_features[i]))
            # Stack results
            return {
                "root_cause_logits": torch.stack([r["root_cause_logits"] for r in batch_results]),
                "risk_score": torch.stack([r["risk_score"] for r in batch_results]),
                "attention_weights": [r["attention_weights"] for r in batch_results],
            }

        # Single graph: node_features shape = (num_services, feat_dim)
        h = self.node_proj(node_features)

        # Get edges (static + learned)
        edge_index, learned_probs = self._get_edge_index(node_features)
        edge_index = edge_index.to(h.device)

        attention_weights = {}

        # GAT Layer 1
        h, attn1 = self.gat1(h, edge_index)
        h = self.norm1(h)
        attention_weights["layer1"] = attn1.detach()

        # GAT Layer 2
        h, attn2 = self.gat2(h, edge_index)
        h = self.norm2(h)
        attention_weights["layer2"] = attn2.detach()

        # GAT Layer 3
        h, attn3 = self.gat3(h, edge_index)
        h = self.norm3(h)
        attention_weights["layer3"] = attn3.detach()

        # Graph readout (attention-weighted pooling)
        readout_weights = F.softmax(self.readout_attn(h), dim=0)  # (num_services, 1)
        graph_repr = (h * readout_weights).sum(dim=0)  # (output_dim,)

        # Head 1: Root cause prediction
        root_cause_logits = self.root_cause_head(graph_repr)  # (num_services,)

        # Head 2: Failure propagation (per edge)
        src, tgt = edge_index
        edge_feats = torch.cat([h[src], h[tgt]], dim=-1)  # (num_edges, 2*output_dim)
        prop_probs = self.propagation_head(edge_feats).squeeze(-1)  # (num_edges,)

        # Head 3: Risk score
        risk_score = self.risk_head(graph_repr).squeeze(-1)  # scalar

        result = {
            "root_cause_logits": root_cause_logits,
            "propagation_probs": prop_probs,
            "risk_score": risk_score,
            "attention_weights": attention_weights,
            "edge_index": edge_index,
        }

        if learned_probs is not None:
            result["learned_edge_probs"] = learned_probs

        return result


# ─────────────────────────────────────────────────────────────────────
# Service feature aggregator
# ─────────────────────────────────────────────────────────────────────


class ServiceFeatureAggregator(nn.Module):
    """Aggregates per-entry features into per-service node features for the GAT.

    Takes a window of log entries and produces a feature vector for each
    service by pooling (mean + max) entries belonging to that service.
    """

    def __init__(self, entry_dim: int, output_dim: int) -> None:
        super().__init__()
        # mean + max pooling → 2x entry_dim, then project
        self.proj = nn.Sequential(
            nn.Linear(2 * entry_dim, output_dim),
            nn.ReLU(),
            nn.LayerNorm(output_dim),
        )
        self.default_feature = nn.Parameter(torch.randn(2 * entry_dim) * 0.01)

    def forward(
        self,
        entry_features: torch.Tensor,   # (seq_len, entry_dim)
        service_indices: torch.Tensor,   # (seq_len,)
        num_services: int = NUM_SERVICES,
    ) -> torch.Tensor:
        """Aggregate entry features to per-service features.

        Returns:
            Tensor of shape ``(num_services, output_dim)``.
        """
        device = entry_features.device
        aggregated = []

        for svc_idx in range(num_services):
            mask = service_indices == svc_idx
            if mask.any():
                svc_feats = entry_features[mask]  # (n_entries, entry_dim)
                mean_pool = svc_feats.mean(dim=0)
                max_pool = svc_feats.max(dim=0).values
                combined = torch.cat([mean_pool, max_pool])  # (2 * entry_dim,)
            else:
                combined = self.default_feature

            aggregated.append(combined)

        stacked = torch.stack(aggregated)  # (num_services, 2 * entry_dim)
        return self.proj(stacked)  # (num_services, output_dim)

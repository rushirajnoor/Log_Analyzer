"""
Custom Transformer Encoder with multi-task classification heads.

Architecture:
    ┌──────────────────────────┐
    │   Input Embedding Layer  │  (input_dim → d_model via Linear)
    └────────────┬─────────────┘
                 │
    ┌────────────▼─────────────┐
    │  [CLS] Token Prepend     │  (optional, learnable)
    └────────────┬─────────────┘
                 │
    ┌────────────▼─────────────┐
    │  Sinusoidal Pos Encoding │  (fixed, Vaswani-style)
    │  + Service Pos Encoding  │  (learned, per-service)
    └────────────┬─────────────┘
                 │
    ┌────────────▼─────────────┐
    │  Transformer Encoder     │  (num_layers=4, nhead=8)
    │  (returns attn weights)  │
    └────────────┬─────────────┘
                 │  [CLS] pooling
    ┌────────────▼─────────────┐
    │  Multi-Task Heads:       │
    │  ├─ Severity Classifier  │  → 3 classes (INFO/WARN/ERROR)
    │  ├─ Fault Type Classifier│  → 6 classes
    │  └─ Severity Regressor   │  → scalar [0, 1]
    └──────────────────────────┘

Design choices:
    - Sinusoidal + learned service positional encoding: sinusoidal handles
      temporal position within the window, while a learned service embedding
      captures which microservice each log came from.
    - Multi-task heads share the encoder to amortize computation and learn
      richer internal representations.
    - Attention weights are returned for explainability/visualization.
    - Pre-LN (norm_first=True) for training stability at moderate depth.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from nn_pipeline.config import (
    TransformerConfig,
    transformer_config,
    NUM_SERVICES,
)


# ─────────────────────────────────────────────────────────────────────
# Sinusoidal positional encoding
# ─────────────────────────────────────────────────────────────────────
class SinusoidalPositionalEncoding(nn.Module):
    """Fixed sinusoidal positional encoding (Vaswani et al., 2017).

    Registers a non-learnable buffer of shape (1, max_seq_len, d_model)
    that encodes absolute position within the input window.
    """

    def __init__(self, d_model: int, max_len: int = 512) -> None:
        super().__init__()
        pe = torch.zeros(max_len, d_model)  # (max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float32).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2, dtype=torch.float32)
            * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # (1, max_len, d_model)
        self.register_buffer("pe", pe)

    def forward(self, seq_len: int) -> Tensor:
        """Return positional encoding for the first seq_len positions.

        Args:
            seq_len: Number of positions to return.

        Returns:
            (1, seq_len, d_model) positional encoding tensor.
        """
        return self.pe[:, :seq_len, :]  # type: ignore[index]


# ─────────────────────────────────────────────────────────────────────
# Multi-task classification head
# ─────────────────────────────────────────────────────────────────────
class ClassificationHead(nn.Module):
    """2-layer MLP classification/regression head.

    Args:
        input_dim: Dimension of the input features.
        hidden_dim: Hidden layer dimension.
        output_dim: Number of output units (classes or 1 for regression).
        dropout: Dropout rate.
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dim: int,
        output_dim: int,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, output_dim),
        )

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: (B, input_dim)

        Returns:
            (B, output_dim)
        """
        return self.net(x)


# ─────────────────────────────────────────────────────────────────────
# Custom Transformer Encoder Layer with attention weight capture
# ─────────────────────────────────────────────────────────────────────
class TransformerEncoderLayerWithAttn(nn.Module):
    """Transformer encoder layer that returns attention weights.

    Standard PyTorch TransformerEncoderLayer doesn't expose attention
    weights easily. This reimplements it with explicit MultiheadAttention
    so we can capture and return the attention maps for explainability.

    Args:
        d_model: Embedding dimension.
        nhead: Number of attention heads.
        dim_feedforward: FFN hidden dimension.
        dropout: Dropout rate.
    """

    def __init__(
        self,
        d_model: int,
        nhead: int,
        dim_feedforward: int,
        dropout: float,
    ) -> None:
        super().__init__()

        self.self_attn = nn.MultiheadAttention(
            embed_dim=d_model,
            num_heads=nhead,
            dropout=dropout,
            batch_first=True,
        )

        # Feed-forward network
        self.ffn = nn.Sequential(
            nn.Linear(d_model, dim_feedforward),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim_feedforward, d_model),
            nn.Dropout(dropout),
        )

        # Pre-LN architecture
        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)

    def forward(
        self,
        src: Tensor,
        src_key_padding_mask: Optional[Tensor] = None,
    ) -> Tuple[Tensor, Tensor]:
        """Forward pass with attention weight capture.

        Args:
            src: (B, S, D) input tensor.
            src_key_padding_mask: (B, S) True where padded.

        Returns:
            Tuple of:
                - (B, S, D) output tensor.
                - (B, nhead, S, S) attention weights.
        """
        # Pre-norm self-attention
        src_norm = self.norm1(src)  # (B, S, D)
        attn_out, attn_weights = self.self_attn(
            src_norm, src_norm, src_norm,
            key_padding_mask=src_key_padding_mask,
            need_weights=True,
            average_attn_weights=False,  # Return per-head weights
        )
        # attn_out: (B, S, D), attn_weights: (B, nhead, S, S)
        src = src + self.dropout1(attn_out)  # (B, S, D)

        # Pre-norm FFN
        src_norm = self.norm2(src)  # (B, S, D)
        src = src + self.ffn(src_norm)  # (B, S, D)

        return src, attn_weights


# ─────────────────────────────────────────────────────────────────────
# Main Transformer classifier model
# ─────────────────────────────────────────────────────────────────────
class TransformerLogClassifier(nn.Module):
    """Transformer Encoder for multi-task log classification.

    Processes a window of log entries (each represented as a feature vector)
    and produces three outputs simultaneously:
        1. Severity classification (INFO / WARNING / ERROR)
        2. Fault type classification (6 fault categories)
        3. Severity score regression (continuous 0-1)

    The model also returns per-layer attention weights for explainability,
    allowing visualization of which log entries attend to which others.

    Args:
        input_dim: Per-log-entry feature dimension (from feature engineering).
        config: TransformerConfig with architecture hyperparameters.
        num_services: Number of services for service positional encoding.
    """

    def __init__(
        self,
        input_dim: int,
        config: TransformerConfig = transformer_config,
        num_services: int = NUM_SERVICES,
    ) -> None:
        super().__init__()
        self.config = config
        self.d_model = config.d_model

        # ── Input projection ──
        # Project per-entry features to d_model
        self.input_projection = nn.Sequential(
            nn.Linear(input_dim, config.d_model),
            nn.GELU(),
            nn.LayerNorm(config.d_model),
        )  # (B, S, input_dim) → (B, S, d_model=128)

        # ── [CLS] token ──
        if config.use_cls_token:
            self.cls_token = nn.Parameter(
                torch.randn(1, 1, config.d_model) * 0.02
            )  # (1, 1, D)

        # ── Positional encodings ──
        # Sinusoidal for temporal position
        self.sinusoidal_pe = SinusoidalPositionalEncoding(
            d_model=config.d_model,
            max_len=config.max_seq_len + 1,  # +1 for [CLS]
        )

        # Learned service positional encoding
        self.service_embedding = nn.Embedding(
            num_embeddings=num_services + 1,  # +1 for unknown service
            embedding_dim=config.d_model,
        )

        # ── Transformer encoder layers ──
        self.encoder_layers = nn.ModuleList([
            TransformerEncoderLayerWithAttn(
                d_model=config.d_model,
                nhead=config.nhead,
                dim_feedforward=config.dim_feedforward,
                dropout=config.dropout,
            )
            for _ in range(config.num_layers)
        ])

        # Final layer norm
        self.final_norm = nn.LayerNorm(config.d_model)

        # ── Multi-task heads ──
        head_hidden = config.d_model * 2  # 256

        self.severity_head = ClassificationHead(
            input_dim=config.d_model,
            hidden_dim=head_hidden,
            output_dim=config.num_severity_classes,  # 3
            dropout=config.dropout,
        )

        self.fault_head = ClassificationHead(
            input_dim=config.d_model,
            hidden_dim=head_hidden,
            output_dim=config.num_fault_classes,  # 6
            dropout=config.dropout,
        )

        self.severity_score_head = ClassificationHead(
            input_dim=config.d_model,
            hidden_dim=head_hidden,
            output_dim=1,  # regression
            dropout=config.dropout,
        )

        # Initialize weights
        self._init_weights()

    def _init_weights(self) -> None:
        """Xavier uniform initialization for linear layers."""
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
            elif isinstance(module, nn.Embedding):
                nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(
        self,
        x: Tensor,
        service_ids: Optional[Tensor] = None,
        padding_mask: Optional[Tensor] = None,
        return_attention: bool = True,
    ) -> Dict[str, Tensor]:
        """Forward pass through the Transformer classifier.

        Args:
            x: (B, S, input_dim) log entry features for the window.
            service_ids: (B, S) integer service indices [0, NUM_SERVICES).
                         If None, service positional encoding is skipped.
            padding_mask: (B, S) True where entries are padded (no real log).
            return_attention: Whether to return attention weight maps.

        Returns:
            Dictionary with keys:
                - "severity_logits": (B, 3) logits for severity classification
                - "fault_logits": (B, 6) logits for fault type classification
                - "severity_score": (B, 1) regression score in [0, 1]
                - "attention_weights": list of (B, nhead, S', S') per layer
                  (only if return_attention=True)
                - "cls_representation": (B, D) the [CLS] token representation

        Shape walkthrough:
            Input x:                  (B, S=64, input_dim)
            After projection:         (B, S, D=128)
            After [CLS] prepend:      (B, S+1, D)
            After positional enc:     (B, S+1, D)
            After encoder layers:     (B, S+1, D)
            [CLS] extraction:         (B, D)
            Severity logits:          (B, 3)
            Fault logits:             (B, 6)
            Severity score:           (B, 1)
        """
        B, S, _ = x.shape  # noqa: N806

        # ── Input projection ──
        x = self.input_projection(x)  # (B, S, D=128)

        # ── Prepend [CLS] token ──
        if self.config.use_cls_token:
            cls_tokens = self.cls_token.expand(B, -1, -1)  # (B, 1, D)
            x = torch.cat([cls_tokens, x], dim=1)  # (B, S+1, D)

            # Update padding mask for [CLS]
            if padding_mask is not None:
                cls_pad = torch.zeros(
                    B, 1, dtype=torch.bool, device=x.device
                )
                padding_mask = torch.cat([cls_pad, padding_mask], dim=1)  # (B, S+1)

            # Update service_ids for [CLS] (use num_services as "unknown")
            if service_ids is not None:
                cls_service = torch.full(
                    (B, 1), NUM_SERVICES,
                    dtype=torch.long, device=x.device,
                )
                service_ids = torch.cat([cls_service, service_ids], dim=1)  # (B, S+1)

            S_total = S + 1  # noqa: N806
        else:
            S_total = S  # noqa: N806

        # ── Add sinusoidal positional encoding ──
        x = x + self.sinusoidal_pe(S_total)  # (B, S_total, D)

        # ── Add service positional encoding ──
        if service_ids is not None:
            service_emb = self.service_embedding(service_ids)  # (B, S_total, D)
            x = x + service_emb  # (B, S_total, D)

        # ── Transformer encoder with attention capture ──
        all_attn_weights = []
        for layer in self.encoder_layers:
            x, attn_w = layer(x, src_key_padding_mask=padding_mask)
            # x: (B, S_total, D), attn_w: (B, nhead, S_total, S_total)
            if return_attention:
                all_attn_weights.append(attn_w)

        x = self.final_norm(x)  # (B, S_total, D)

        # ── Extract [CLS] representation ──
        if self.config.use_cls_token:
            cls_repr = x[:, 0, :]  # (B, D=128)
        else:
            # Mean pooling fallback
            if padding_mask is not None:
                mask = ~padding_mask.unsqueeze(-1)  # (B, S_total, 1)
                cls_repr = (x * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
            else:
                cls_repr = x.mean(dim=1)  # (B, D)

        # ── Multi-task heads ──
        severity_logits = self.severity_head(cls_repr)      # (B, 3)
        fault_logits = self.fault_head(cls_repr)             # (B, 6)
        severity_score = torch.sigmoid(
            self.severity_score_head(cls_repr)
        )  # (B, 1) → [0, 1]

        result: Dict[str, Tensor] = {
            "severity_logits": severity_logits,
            "fault_logits": fault_logits,
            "severity_score": severity_score,
            "cls_representation": cls_repr,
        }

        if return_attention:
            result["attention_weights"] = torch.stack(all_attn_weights, dim=0)
            # Shape: (num_layers, B, nhead, S_total, S_total)

        return result

    def get_attention_rollout(
        self,
        attention_weights: Tensor,
    ) -> Tensor:
        """Compute attention rollout for interpretability.

        Multiplies attention matrices across layers to get an approximation
        of information flow from input tokens to the [CLS] token.

        Args:
            attention_weights: (num_layers, B, nhead, S, S) from forward().

        Returns:
            (B, S) attention rollout scores showing each position's
            contribution to the [CLS] representation.
        """
        # Average over heads → (num_layers, B, S, S)
        attn = attention_weights.mean(dim=2)

        # Add residual connections (identity)
        num_layers, B, S, _ = attn.shape
        eye = torch.eye(S, device=attn.device).unsqueeze(0).unsqueeze(0)
        attn = attn + eye  # (num_layers, B, S, S)

        # Re-normalize
        attn = attn / attn.sum(dim=-1, keepdim=True)

        # Multiply across layers
        rollout = attn[0]  # (B, S, S)
        for i in range(1, num_layers):
            rollout = torch.bmm(attn[i], rollout)  # (B, S, S)

        # Extract [CLS] row (position 0) → (B, S)
        cls_attention = rollout[:, 0, :]
        return cls_attention

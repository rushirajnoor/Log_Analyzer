"""
Contrastive learning model (SimCLR-style) for learning log embeddings.

Architecture:
    ┌─────────────────┐
    │ Token Embedding  │  (token_vocab_size → embedding_dim)
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  Positional Enc  │  (sinusoidal, max_tokens_per_template)
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │ TransformerEnc   │  (encoder_layers, encoder_heads)
    └────────┬────────┘
             │  [CLS] pooling
    ┌────────▼────────┐
    │  Projection Head │  (embedding_dim → projection_dim, 2-layer MLP)
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │   NT-Xent Loss   │  (contrastive, temperature τ)
    └─────────────────┘

Training procedure:
    For each log template, generate two augmented views via random token
    dropout. The model learns to pull views of the same template together
    and push different templates apart in the projection space.

After pre-training, discard the projection head and use the encoder
backbone to produce fixed-size log embeddings (embedding_dim=128).

Design choices:
    - Token dropout augmentation is simple yet effective for log data
      where word order is highly structured (unlike NL).
    - [CLS]-style pooling instead of mean-pooling gives the model a
      dedicated aggregation token.
    - Small projection head (2-layer, 32-dim) follows SimCLR findings
      that the projection head can be small/discarded.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from nn_pipeline.config import contrastive_config, ContrastiveConfig
from nn_pipeline.embeddings.template_vocab import (
    PAD_IDX,
    CLS_IDX,
    TemplateVocabulary,
)


# ─────────────────────────────────────────────────────────────────────
# Sinusoidal positional encoding (fixed, no learned parameters)
# ─────────────────────────────────────────────────────────────────────
class SinusoidalPositionalEncoding(nn.Module):
    """Fixed sinusoidal positional encoding (Vaswani et al., 2017).

    Generates a buffer of shape (1, max_len, d_model) that is added to
    token embeddings before entering the Transformer encoder.
    """

    def __init__(self, d_model: int, max_len: int = 512, dropout: float = 0.1) -> None:
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        # (max_len, d_model)
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float32).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2, dtype=torch.float32)
            * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # (1, max_len, d_model)

        self.register_buffer("pe", pe)

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: (batch, seq_len, d_model)

        Returns:
            Positionally encoded tensor of same shape.
        """
        # x shape: (B, S, D)
        x = x + self.pe[:, : x.size(1), :]  # type: ignore[index]
        return self.dropout(x)


# ─────────────────────────────────────────────────────────────────────
# Token dropout augmentation
# ─────────────────────────────────────────────────────────────────────
def token_dropout_augment(
    token_ids: Tensor,
    dropout_rate: float,
    pad_idx: int = PAD_IDX,
) -> Tensor:
    """Randomly replace tokens with PAD to create an augmented view.

    This simulates partial observation of a log template—a lightweight
    augmentation that forces the encoder to be robust to missing tokens.

    Args:
        token_ids: (batch, seq_len) integer token IDs.
        dropout_rate: Fraction of non-pad, non-CLS tokens to drop.
        pad_idx: Index used for padding (dropped tokens become this).

    Returns:
        Augmented copy of token_ids with some tokens replaced by pad_idx.
    """
    augmented = token_ids.clone()
    # Mask: True where we *could* drop (non-pad, non-CLS)
    droppable = (token_ids != pad_idx) & (token_ids != CLS_IDX)
    # Random mask
    drop_mask = torch.rand_like(token_ids, dtype=torch.float32) < dropout_rate
    augmented[droppable & drop_mask] = pad_idx
    return augmented


# ─────────────────────────────────────────────────────────────────────
# Log2Vec encoder backbone
# ─────────────────────────────────────────────────────────────────────
class Log2VecEncoder(nn.Module):
    """Transformer encoder backbone for log template embedding.

    Converts a sequence of token IDs (from a log template) into a
    fixed-size dense vector via [CLS] token pooling.

    Args:
        token_vocab_size: Size of the token-level vocabulary.
        config: ContrastiveConfig with architecture hyperparameters.
    """

    def __init__(
        self,
        token_vocab_size: int,
        config: ContrastiveConfig = contrastive_config,
    ) -> None:
        super().__init__()
        self.config = config

        # Token embedding
        self.token_embedding = nn.Embedding(
            num_embeddings=token_vocab_size,
            embedding_dim=config.embedding_dim,
            padding_idx=PAD_IDX,
        )  # (V, D=128)

        # Positional encoding
        self.pos_encoder = SinusoidalPositionalEncoding(
            d_model=config.embedding_dim,
            max_len=config.max_tokens_per_template + 1,  # +1 for [CLS]
        )

        # Transformer encoder
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=config.embedding_dim,     # 128
            nhead=config.encoder_heads,        # 4
            dim_feedforward=config.embedding_dim * 4,  # 512
            dropout=0.1,
            activation="gelu",
            batch_first=True,
            norm_first=True,  # Pre-LN for training stability
        )
        self.transformer_encoder = nn.TransformerEncoder(
            encoder_layer=encoder_layer,
            num_layers=config.encoder_layers,  # 2
            enable_nested_tensor=False,
        )

        # Layer norm on final representation
        self.output_norm = nn.LayerNorm(config.embedding_dim)

    def forward(
        self,
        token_ids: Tensor,
        return_all_tokens: bool = False,
    ) -> Tensor:
        """Encode token sequences into dense vectors.

        Args:
            token_ids: (B, S) integer token IDs, where S includes the
                       leading [CLS] token.
            return_all_tokens: If True, return all token representations
                               (B, S, D). Otherwise return only [CLS] (B, D).

        Returns:
            If return_all_tokens: (B, S, D) tensor
            Otherwise: (B, D) tensor — the [CLS] representation.
        """
        B, S = token_ids.shape  # noqa: N806

        # Padding mask: True where padded (for src_key_padding_mask)
        padding_mask = (token_ids == PAD_IDX)  # (B, S)

        # Embed tokens → (B, S, D=128)
        x = self.token_embedding(token_ids)

        # Scale by sqrt(d_model) for stability
        x = x * math.sqrt(self.config.embedding_dim)

        # Add positional encoding → (B, S, D)
        x = self.pos_encoder(x)

        # Transformer encoder → (B, S, D)
        x = self.transformer_encoder(
            x,
            src_key_padding_mask=padding_mask,
        )

        if return_all_tokens:
            return x

        # [CLS] pooling: take the first token → (B, D)
        cls_repr = x[:, 0, :]  # (B, D=128)
        cls_repr = self.output_norm(cls_repr)

        return cls_repr


# ─────────────────────────────────────────────────────────────────────
# Projection head
# ─────────────────────────────────────────────────────────────────────
class ProjectionHead(nn.Module):
    """2-layer MLP projection head for contrastive learning.

    Maps the encoder output (embedding_dim) down to a smaller
    projection space (projection_dim) where the NT-Xent loss operates.
    Following SimCLR, this head is discarded after pre-training.

    Args:
        input_dim: Encoder output dimension (embedding_dim).
        projection_dim: Contrastive projection dimension.
    """

    def __init__(self, input_dim: int, projection_dim: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, input_dim),
            nn.BatchNorm1d(input_dim),
            nn.ReLU(inplace=True),
            nn.Linear(input_dim, projection_dim),
        )

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: (B, input_dim)

        Returns:
            (B, projection_dim) L2-normalized projections.
        """
        z = self.net(x)  # (B, projection_dim)
        return F.normalize(z, dim=-1)


# ─────────────────────────────────────────────────────────────────────
# NT-Xent (Normalized Temperature-scaled Cross-Entropy) Loss
# ─────────────────────────────────────────────────────────────────────
class NTXentLoss(nn.Module):
    """NT-Xent contrastive loss (SimCLR, Chen et al. 2020).

    For a batch of N templates, we get 2N augmented views.
    Each template's two views are positives; all other 2(N-1) views
    in the batch are negatives.

    Args:
        temperature: Scaling temperature τ. Lower → sharper distribution.
    """

    def __init__(self, temperature: float = contrastive_config.temperature) -> None:
        super().__init__()
        self.temperature = temperature

    def forward(self, z_i: Tensor, z_j: Tensor) -> Tensor:
        """Compute NT-Xent loss for two sets of projections.

        Args:
            z_i: (B, D) L2-normalized projections from view 1.
            z_j: (B, D) L2-normalized projections from view 2.

        Returns:
            Scalar loss.
        """
        B = z_i.size(0)  # noqa: N806
        device = z_i.device

        # Concatenate: (2B, D)
        z = torch.cat([z_i, z_j], dim=0)  # (2B, D)

        # Cosine similarity matrix: (2B, 2B)
        sim_matrix = torch.mm(z, z.t()) / self.temperature  # (2B, 2B)

        # Mask out self-similarity (diagonal)
        mask_self = torch.eye(2 * B, dtype=torch.bool, device=device)
        sim_matrix.masked_fill_(mask_self, float("-inf"))

        # Positive pairs: (i, i+B) and (i+B, i)
        # Labels: for each row i in [0, B), the positive is at column i+B
        #         for each row i in [B, 2B), the positive is at column i-B
        labels = torch.cat([
            torch.arange(B, 2 * B, device=device),  # pos for first half
            torch.arange(0, B, device=device),       # pos for second half
        ])  # (2B,)

        loss = F.cross_entropy(sim_matrix, labels)
        return loss


# ─────────────────────────────────────────────────────────────────────
# Full Log2Vec model (encoder + projection + loss)
# ─────────────────────────────────────────────────────────────────────
class Log2Vec(nn.Module):
    """Complete contrastive learning model for log embeddings.

    Combines the encoder backbone, projection head, and NT-Xent loss
    into a single module for training convenience.

    Usage:
        >>> vocab = TemplateVocabulary(...)
        >>> model = Log2Vec(token_vocab_size=vocab.token_vocab_size)
        >>> # Training: pass raw token_ids, model handles augmentation
        >>> loss = model(token_ids)
        >>> # Inference: get embeddings (projection head discarded)
        >>> embeddings = model.encode(token_ids)

    Args:
        token_vocab_size: Size of the token vocabulary.
        config: ContrastiveConfig hyperparameters.
    """

    def __init__(
        self,
        token_vocab_size: int,
        config: ContrastiveConfig = contrastive_config,
    ) -> None:
        super().__init__()
        self.config = config

        self.encoder = Log2VecEncoder(
            token_vocab_size=token_vocab_size,
            config=config,
        )

        self.projection_head = ProjectionHead(
            input_dim=config.embedding_dim,    # 128
            projection_dim=config.projection_dim,  # 32
        )

        self.loss_fn = NTXentLoss(temperature=config.temperature)

    def forward(self, token_ids: Tensor) -> Tensor:
        """Training forward pass: augment → encode → project → NT-Xent loss.

        Args:
            token_ids: (B, S) integer token IDs (with leading [CLS]).

        Returns:
            Scalar contrastive loss.
        """
        # Generate two augmented views
        view_1 = token_dropout_augment(
            token_ids,
            dropout_rate=self.config.token_dropout_rate,
        )  # (B, S)
        view_2 = token_dropout_augment(
            token_ids,
            dropout_rate=self.config.token_dropout_rate,
        )  # (B, S)

        # Encode both views → (B, D=128)
        h_1 = self.encoder(view_1)  # (B, 128)
        h_2 = self.encoder(view_2)  # (B, 128)

        # Project → (B, projection_dim=32), L2-normalized
        z_1 = self.projection_head(h_1)  # (B, 32)
        z_2 = self.projection_head(h_2)  # (B, 32)

        # Compute NT-Xent loss
        loss = self.loss_fn(z_1, z_2)
        return loss

    @torch.no_grad()
    def encode(self, token_ids: Tensor) -> Tensor:
        """Extract embeddings without the projection head (inference mode).

        Args:
            token_ids: (B, S) integer token IDs.

        Returns:
            (B, embedding_dim=128) dense embeddings.
        """
        self.eval()
        return self.encoder(token_ids)  # (B, 128)

    @torch.no_grad()
    def encode_with_projection(self, token_ids: Tensor) -> Tensor:
        """Extract projected embeddings (for visualization / nearest neighbor).

        Args:
            token_ids: (B, S) integer token IDs.

        Returns:
            (B, projection_dim=32) L2-normalized projections.
        """
        self.eval()
        h = self.encoder(token_ids)   # (B, 128)
        z = self.projection_head(h)   # (B, 32)
        return z

    def get_encoder(self) -> Log2VecEncoder:
        """Return the encoder backbone (without projection head).

        Use this to extract the pre-trained encoder for downstream tasks
        (e.g., feeding into the Transformer classifier).
        """
        return self.encoder

"""
Bidirectional LSTM Autoencoder for unsupervised anomaly detection.

Architecture:
    ┌─────────────────────────┐
    │  Input (B, S, input_dim)│
    └────────────┬────────────┘
                 │
    ┌────────────▼────────────┐
    │  BiLSTM Encoder         │  (num_layers=2, hidden=64, bidir)
    │  → (B, S, 2*hidden)     │
    └────────────┬────────────┘
                 │  last hidden state
    ┌────────────▼────────────┐
    │  Bottleneck (Linear)    │  (2*hidden → bottleneck_dim=32)
    └────────────┬────────────┘
                 │  repeat to seq_len
    ┌────────────▼────────────┐
    │  LSTM Decoder           │  (num_layers=2, hidden=64)
    └────────────┬────────────┘
                 │
    ┌────────────▼────────────┐
    │  Output Linear          │  (hidden → input_dim)
    └─────────────────────────┘

Anomaly detection:
    Score = MSE(input, reconstruction)
    Threshold = percentile(training_scores, anomaly_percentile)
    Entries with score > threshold are flagged as anomalous.

Design choices:
    - Bidirectional encoder captures both forward/backward temporal
      context in the log sequence.
    - Bottleneck forces information compression — normal patterns are
      well reconstructed, anomalies yield high reconstruction error.
    - Unidirectional decoder prevents information leakage.
    - Threshold is computed from training data reconstruction errors
      at a configurable percentile (default 95th).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn
from torch import Tensor
import numpy as np

from nn_pipeline.config import (
    LSTMAutoencoderConfig,
    lstm_ae_config,
)


class LSTMEncoder(nn.Module):
    """Bidirectional LSTM encoder.

    Takes a sequence of feature vectors and produces a compressed
    bottleneck representation.

    Args:
        input_dim: Per-timestep input feature dimension.
        hidden_dim: LSTM hidden dimension (per direction).
        bottleneck_dim: Output bottleneck dimension.
        num_layers: Number of stacked LSTM layers.
        bidirectional: Use bidirectional LSTM.
        dropout: Dropout between LSTM layers.
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dim: int,
        bottleneck_dim: int,
        num_layers: int,
        bidirectional: bool = True,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.bidirectional = bidirectional
        self.num_directions = 2 if bidirectional else 1

        self.lstm = nn.LSTM(
            input_size=input_dim,       # 20
            hidden_size=hidden_dim,     # 64
            num_layers=num_layers,      # 2
            batch_first=True,
            bidirectional=bidirectional,
            dropout=dropout if num_layers > 1 else 0.0,
        )

        # Project concatenated final hidden states → bottleneck
        encoder_output_dim = hidden_dim * self.num_directions  # 128 if bidir
        self.bottleneck = nn.Sequential(
            nn.Linear(encoder_output_dim, bottleneck_dim),
            nn.Tanh(),
        )  # (B, encoder_output_dim) → (B, bottleneck_dim=32)

    def forward(self, x: Tensor) -> Tuple[Tensor, Tensor]:
        """Encode the input sequence.

        Args:
            x: (B, S, input_dim) input sequence.

        Returns:
            Tuple of:
                - bottleneck: (B, bottleneck_dim) compressed representation.
                - encoder_outputs: (B, S, hidden_dim * num_directions) full
                  sequence of encoder hidden states.
        """
        # LSTM forward pass
        # encoder_outputs: (B, S, hidden_dim * num_directions)
        # h_n: (num_layers * num_directions, B, hidden_dim)
        encoder_outputs, (h_n, _c_n) = self.lstm(x)

        # Concatenate the final forward and backward hidden states
        # from the last layer
        if self.bidirectional:
            # h_n shape: (num_layers*2, B, hidden_dim)
            # Last layer forward: h_n[-2], Last layer backward: h_n[-1]
            h_forward = h_n[-2]   # (B, hidden_dim)
            h_backward = h_n[-1]  # (B, hidden_dim)
            h_combined = torch.cat([h_forward, h_backward], dim=-1)
            # (B, hidden_dim * 2 = 128)
        else:
            h_combined = h_n[-1]  # (B, hidden_dim)

        # Compress to bottleneck
        bottleneck = self.bottleneck(h_combined)  # (B, bottleneck_dim=32)

        return bottleneck, encoder_outputs


class LSTMDecoder(nn.Module):
    """LSTM decoder for sequence reconstruction.

    Takes the bottleneck representation and reconstructs the original
    input sequence.

    Args:
        bottleneck_dim: Input dimension from the bottleneck.
        hidden_dim: LSTM hidden dimension.
        output_dim: Per-timestep output dimension (should match input_dim).
        num_layers: Number of stacked LSTM layers.
        seq_len: Length of the sequence to reconstruct.
        dropout: Dropout between LSTM layers.
    """

    def __init__(
        self,
        bottleneck_dim: int,
        hidden_dim: int,
        output_dim: int,
        num_layers: int,
        seq_len: int,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.seq_len = seq_len
        self.hidden_dim = hidden_dim

        # Expand bottleneck to decoder input
        self.bottleneck_expand = nn.Sequential(
            nn.Linear(bottleneck_dim, hidden_dim),
            nn.ReLU(inplace=True),
        )  # (B, bottleneck_dim) → (B, hidden_dim)

        # Decoder LSTM (unidirectional — no information leakage)
        self.lstm = nn.LSTM(
            input_size=hidden_dim,      # 64
            hidden_size=hidden_dim,     # 64
            num_layers=num_layers,      # 2
            batch_first=True,
            bidirectional=False,
            dropout=dropout if num_layers > 1 else 0.0,
        )

        # Output projection to reconstruct input features
        self.output_projection = nn.Linear(hidden_dim, output_dim)
        # (B, S, hidden_dim) → (B, S, output_dim=input_dim)

    def forward(self, bottleneck: Tensor) -> Tensor:
        """Decode bottleneck into reconstructed sequence.

        Args:
            bottleneck: (B, bottleneck_dim) compressed representation.

        Returns:
            (B, S, output_dim) reconstructed sequence.
        """
        B = bottleneck.size(0)  # noqa: N806

        # Expand bottleneck
        expanded = self.bottleneck_expand(bottleneck)  # (B, hidden_dim)

        # Repeat across time steps to form decoder input
        decoder_input = expanded.unsqueeze(1).repeat(
            1, self.seq_len, 1
        )  # (B, S, hidden_dim)

        # Decode
        decoder_outputs, _ = self.lstm(decoder_input)
        # decoder_outputs: (B, S, hidden_dim)

        # Project to output dimension
        reconstructed = self.output_projection(decoder_outputs)
        # (B, S, output_dim=input_dim)

        return reconstructed


class LSTMAutoencoder(nn.Module):
    """Bidirectional LSTM Autoencoder for unsupervised anomaly detection.

    The model learns to reconstruct normal log sequences. During inference,
    anomalous sequences produce higher reconstruction error (MSE),
    which serves as the anomaly score.

    Args:
        config: LSTMAutoencoderConfig with architecture hyperparameters.

    Example:
        >>> config = LSTMAutoencoderConfig()
        >>> model = LSTMAutoencoder(config)
        >>> x = torch.randn(32, 64, 20)  # (batch, seq_len, features)
        >>> result = model(x)
        >>> print(result["reconstruction_error"].shape)  # (32,)
    """

    def __init__(
        self,
        config: LSTMAutoencoderConfig = lstm_ae_config,
    ) -> None:
        super().__init__()
        self.config = config

        # Encoder
        self.encoder = LSTMEncoder(
            input_dim=config.input_dim,         # 20
            hidden_dim=config.hidden_dim,       # 64
            bottleneck_dim=config.bottleneck_dim,  # 32
            num_layers=config.num_layers,       # 2
            bidirectional=config.bidirectional,  # True
            dropout=config.dropout,
        )

        # Decoder
        self.decoder = LSTMDecoder(
            bottleneck_dim=config.bottleneck_dim,  # 32
            hidden_dim=config.hidden_dim,          # 64
            output_dim=config.input_dim,           # 20
            num_layers=config.num_layers,          # 2
            seq_len=config.seq_len,                # 64
            dropout=config.dropout,
        )

        # Anomaly threshold (set during training via compute_threshold)
        self.register_buffer(
            "anomaly_threshold",
            torch.tensor(float("inf")),
        )

        # Initialize weights
        self._init_weights()

    def _init_weights(self) -> None:
        """Initialize LSTM and linear layer weights."""
        for name, param in self.named_parameters():
            if "weight_ih" in name:
                nn.init.xavier_uniform_(param)
            elif "weight_hh" in name:
                nn.init.orthogonal_(param)
            elif "bias" in name:
                nn.init.zeros_(param)
            elif "weight" in name and param.dim() >= 2:
                nn.init.xavier_uniform_(param)

    def forward(self, x: Tensor) -> Dict[str, Tensor]:
        """Forward pass: encode → bottleneck → decode → compute error.

        Args:
            x: (B, S, input_dim) input log feature sequences.

        Returns:
            Dictionary with keys:
                - "reconstruction": (B, S, input_dim) reconstructed sequence
                - "bottleneck": (B, bottleneck_dim) compressed representation
                - "reconstruction_error": (B,) per-sample MSE
                - "is_anomaly": (B,) boolean anomaly flags

        Shape walkthrough:
            Input:              (B, S=64, D=20)
            Encoder output:     (B, bottleneck_dim=32)
            Decoder output:     (B, S=64, D=20)
            Recon error:        (B,)
        """
        # Encode → (B, 32)
        bottleneck, _encoder_outputs = self.encoder(x)

        # Decode → (B, S=64, input_dim=20)
        reconstruction = self.decoder(bottleneck)

        # Per-sample reconstruction error (MSE across seq and features)
        recon_error = torch.mean(
            (x - reconstruction) ** 2,
            dim=(1, 2),
        )  # (B,)

        # Anomaly detection
        is_anomaly = recon_error > self.anomaly_threshold  # (B,)

        return {
            "reconstruction": reconstruction,
            "bottleneck": bottleneck,
            "reconstruction_error": recon_error,
            "is_anomaly": is_anomaly,
        }

    def encode(self, x: Tensor) -> Tensor:
        """Extract bottleneck representation only.

        Args:
            x: (B, S, input_dim)

        Returns:
            (B, bottleneck_dim) compressed representation.
        """
        bottleneck, _ = self.encoder(x)
        return bottleneck

    @torch.no_grad()
    def compute_anomaly_scores(self, x: Tensor) -> Tensor:
        """Compute anomaly scores (reconstruction error) for a batch.

        Args:
            x: (B, S, input_dim)

        Returns:
            (B,) reconstruction error scores.
        """
        self.eval()
        result = self.forward(x)
        return result["reconstruction_error"]

    @torch.no_grad()
    def compute_threshold(
        self,
        train_loader: "torch.utils.data.DataLoader",  # type: ignore[name-defined]
        percentile: Optional[float] = None,
    ) -> float:
        """Compute anomaly threshold from training data.

        Runs the model over the entire training set, collects reconstruction
        errors, and sets the threshold at the specified percentile.

        Args:
            train_loader: DataLoader yielding batches of (B, S, input_dim).
            percentile: Percentile for threshold (default from config).

        Returns:
            The computed threshold value.
        """
        if percentile is None:
            percentile = self.config.anomaly_percentile

        self.eval()
        all_errors: list[Tensor] = []

        for batch in train_loader:
            if isinstance(batch, (tuple, list)):
                x = batch[0]
            else:
                x = batch

            x = x.to(next(self.parameters()).device)
            result = self.forward(x)
            all_errors.append(result["reconstruction_error"].cpu())

        errors = torch.cat(all_errors)  # (N,)
        threshold = float(np.percentile(errors.numpy(), percentile))

        # Store as buffer
        self.anomaly_threshold.fill_(threshold)

        return threshold

    @staticmethod
    def reconstruction_loss(
        x: Tensor,
        reconstruction: Tensor,
    ) -> Tensor:
        """Compute MSE reconstruction loss for training.

        Args:
            x: (B, S, input_dim) original input.
            reconstruction: (B, S, input_dim) reconstructed output.

        Returns:
            Scalar MSE loss.
        """
        return nn.functional.mse_loss(reconstruction, x)

"""
Conditional Variational Autoencoder (CVAE) for anomaly scoring.

Learns the distribution of normal log features conditioned on service type.
Anomalies are detected as entries with high reconstruction error (low ELBO).

Architecture:
    Input: log features (entry_dim) + service one-hot (num_services)
    → Encoder MLP → μ, log_σ² (latent_dim each)
    → Reparameterization: z = μ + σ * ε
    → Decoder MLP (z + service condition) → reconstructed features
    Loss: BCE/MSE reconstruction + β * KL divergence

Also supports:
    - Synthetic log generation by sampling from the latent space.
    - Calibrated anomaly scores via -ELBO.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from nn_pipeline.config import vae_config as cfg, NUM_SERVICES


class Encoder(nn.Module):
    """VAE Encoder: maps input + condition to latent distribution parameters."""

    def __init__(self, input_dim: int, hidden_dim: int, latent_dim: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim),
        )
        self.fc_mu = nn.Linear(hidden_dim, latent_dim)
        self.fc_logvar = nn.Linear(hidden_dim, latent_dim)

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """Encode input to latent distribution.

        Args:
            x: Input tensor, shape ``(batch, input_dim)``.

        Returns:
            Tuple of (mu, log_var), each shape ``(batch, latent_dim)``.
        """
        h = self.net(x)
        return self.fc_mu(h), self.fc_logvar(h)


class Decoder(nn.Module):
    """VAE Decoder: maps latent code + condition to reconstructed input."""

    def __init__(self, latent_dim: int, condition_dim: int, hidden_dim: int, output_dim: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(latent_dim + condition_dim, hidden_dim),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim),
            nn.Linear(hidden_dim, output_dim),
            nn.Sigmoid(),  # Output in [0, 1] range
        )

    def forward(self, z: torch.Tensor, condition: torch.Tensor) -> torch.Tensor:
        """Decode latent code to reconstructed features.

        Args:
            z: Latent code, shape ``(batch, latent_dim)``.
            condition: Service one-hot, shape ``(batch, condition_dim)``.

        Returns:
            Reconstructed features, shape ``(batch, output_dim)``.
        """
        return self.net(torch.cat([z, condition], dim=-1))


class ConditionalVAE(nn.Module):
    """Conditional Variational Autoencoder for log anomaly scoring.

    The model learns P(features | service) from normal log data.
    At inference time, anomaly score = -ELBO = recon_loss + KL_div.

    Args:
        config: VAEConfig with architecture parameters.
    """

    def __init__(self, config=None) -> None:
        super().__init__()
        c = config or cfg

        self.latent_dim = c.latent_dim
        self.beta = c.beta
        self.condition_dim = c.condition_dim

        # The encoder takes features + condition
        feature_dim = c.input_dim - c.condition_dim  # Raw feature dim (without service)
        self.feature_dim = feature_dim
        encoder_input = c.input_dim  # features + condition

        self.encoder = Encoder(encoder_input, c.hidden_dim, c.latent_dim)
        self.decoder = Decoder(c.latent_dim, c.condition_dim, c.hidden_dim, feature_dim)

    def reparameterize(self, mu: torch.Tensor, log_var: torch.Tensor) -> torch.Tensor:
        """Reparameterization trick: z = μ + σ * ε.

        Args:
            mu: Mean of latent distribution, shape ``(batch, latent_dim)``.
            log_var: Log variance, shape ``(batch, latent_dim)``.

        Returns:
            Sampled latent code, shape ``(batch, latent_dim)``.
        """
        if self.training:
            std = torch.exp(0.5 * log_var)
            eps = torch.randn_like(std)
            return mu + std * eps
        else:
            return mu  # Use mean at inference (deterministic)

    def forward(
        self,
        features: torch.Tensor,
        service_onehot: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Forward pass: encode → sample → decode.

        Args:
            features: Log feature vector, shape ``(batch, feature_dim)``.
            service_onehot: Service one-hot encoding, shape ``(batch, condition_dim)``.

        Returns:
            Dict with keys:
              - ``reconstructed``: Reconstructed features.
              - ``mu``: Latent mean.
              - ``log_var``: Latent log variance.
              - ``z``: Sampled latent code.
              - ``anomaly_score``: Per-sample anomaly score (-ELBO components).
        """
        # Encode
        encoder_input = torch.cat([features, service_onehot], dim=-1)
        mu, log_var = self.encoder(encoder_input)

        # Sample
        z = self.reparameterize(mu, log_var)

        # Decode
        reconstructed = self.decoder(z, service_onehot)

        # Compute per-sample anomaly scores
        recon_loss = F.mse_loss(reconstructed, features, reduction='none').sum(dim=-1)
        kl_div = -0.5 * (1 + log_var - mu.pow(2) - log_var.exp()).sum(dim=-1)
        anomaly_score = recon_loss + self.beta * kl_div

        return {
            "reconstructed": reconstructed,
            "mu": mu,
            "log_var": log_var,
            "z": z,
            "anomaly_score": anomaly_score,
            "recon_loss": recon_loss,
            "kl_div": kl_div,
        }

    def loss(
        self,
        features: torch.Tensor,
        service_onehot: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Compute VAE loss (reconstruction + β * KL).

        Returns:
            Dict with ``total_loss``, ``recon_loss``, ``kl_loss``.
        """
        output = self.forward(features, service_onehot)
        recon = F.mse_loss(output["reconstructed"], features, reduction='mean')
        kl = -0.5 * torch.mean(1 + output["log_var"] - output["mu"].pow(2) - output["log_var"].exp())
        total = recon + self.beta * kl

        return {
            "total_loss": total,
            "recon_loss": recon,
            "kl_loss": kl,
        }

    @torch.no_grad()
    def generate(
        self,
        service_idx: int,
        num_samples: int = 10,
        device: Optional[torch.device] = None,
    ) -> torch.Tensor:
        """Generate synthetic log features for a given service.

        Samples from the prior N(0, I) and decodes with the service condition.

        Args:
            service_idx: Index of the service to generate for.
            num_samples: Number of samples to generate.
            device: Device for tensors.

        Returns:
            Generated features, shape ``(num_samples, feature_dim)``.
        """
        if device is None:
            device = next(self.parameters()).device

        z = torch.randn(num_samples, self.latent_dim, device=device)
        condition = torch.zeros(num_samples, self.condition_dim, device=device)
        condition[:, service_idx] = 1.0

        self.eval()
        return self.decoder(z, condition)

    @torch.no_grad()
    def compute_anomaly_threshold(
        self,
        features: torch.Tensor,
        service_onehot: torch.Tensor,
        percentile: float = 95.0,
    ) -> float:
        """Compute anomaly threshold from training data.

        Args:
            features: Normal training features.
            service_onehot: Corresponding service encodings.
            percentile: Percentile for threshold (e.g., 95 = top 5% are anomalies).

        Returns:
            Threshold value for anomaly detection.
        """
        self.eval()
        output = self.forward(features, service_onehot)
        scores = output["anomaly_score"].cpu().numpy()
        return float(scores[int(len(scores) * percentile / 100)])

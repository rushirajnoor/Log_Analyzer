"""
Master training script — orchestrates training of all models in the correct order.

Execution order:
    1. Log2Vec contrastive pre-training  (self-supervised embeddings)
    2. Transformer classifier            (severity + fault classification)
    3. LSTM-AE                           (unsupervised anomaly detection)
    4. GAT                               (service-graph root cause analysis)
    5. VAE                               (probabilistic anomaly scoring)
    6. Knowledge distillation            (fast GRU student from Transformer teacher)

Run with::

    python -m nn_pipeline.training.train_all
    python -m nn_pipeline.training.train_all --models transformer lstm_ae
    python -m nn_pipeline.training.train_all --skip contrastive distilled
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from nn_pipeline.config import (
    MODEL_CHECKPOINTS_DIR,
    PROCESSED_DATA_DIR,
    TrainingConfig,
    contrastive_config,
    data_config,
    distilled_config,
    gat_config,
    lstm_ae_config,
    training_config,
    transformer_config,
    vae_config,
)

from nn_pipeline.training.trainer import CheckpointManager, Trainer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Seed everything
# ─────────────────────────────────────────────────────────────────────


def seed_everything(seed: int = 42) -> None:
    """Set random seeds for reproducibility."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


# ─────────────────────────────────────────────────────────────────────
# Device selection
# ─────────────────────────────────────────────────────────────────────


def get_device(cfg: TrainingConfig) -> torch.device:
    """Resolve device with GPU fallback."""
    if cfg.device == "cuda" and torch.cuda.is_available():
        device = torch.device("cuda")
        logger.info("Using GPU: %s", torch.cuda.get_device_name(0))
    else:
        device = torch.device("cpu")
        if cfg.device == "cuda":
            logger.warning("CUDA requested but unavailable — using CPU.")
        else:
            logger.info("Using CPU.")
    return device


# ─────────────────────────────────────────────────────────────────────
# Data loaders (lazy import from nn_pipeline.data)
# ─────────────────────────────────────────────────────────────────────


def _load_data(
    data_type: str,
    device: torch.device,
) -> Tuple[DataLoader, DataLoader, DataLoader]:
    """Load train / val / test DataLoaders.

    Tries to import from nn_pipeline.data.datasets first. Falls back to
    synthetic placeholder data for development / CI.

    Args:
        data_type: One of 'classification', 'anomaly', 'graph', 'contrastive'.
        device: Target device (used for tensor creation in fallback).

    Returns:
        (train_loader, val_loader, test_loader)
    """
    try:
        from nn_pipeline.data.datasets import get_dataloaders

        return get_dataloaders(data_type)
    except ImportError:
        logger.warning(
            "nn_pipeline.data.datasets not available yet — using synthetic data."
        )
        return _synthetic_loaders(data_type)


def _synthetic_loaders(
    data_type: str,
) -> Tuple[DataLoader, DataLoader, DataLoader]:
    """Generate tiny synthetic DataLoaders for testing the training loop."""
    bs = data_config.batch_size
    seq_len = data_config.window_size
    feat_dim = data_config.per_entry_feature_dim
    n_train, n_val, n_test = 256, 64, 64

    if data_type == "classification":
        # (batch, seq_len, feat_dim) → (batch,) labels
        x_train = torch.randn(n_train, seq_len, feat_dim)
        y_train = torch.randint(0, transformer_config.num_fault_classes, (n_train,))
        x_val = torch.randn(n_val, seq_len, feat_dim)
        y_val = torch.randint(0, transformer_config.num_fault_classes, (n_val,))
        x_test = torch.randn(n_test, seq_len, feat_dim)
        y_test = torch.randint(0, transformer_config.num_fault_classes, (n_test,))
    elif data_type == "anomaly":
        x_train = torch.randn(n_train, seq_len, feat_dim)
        y_train = torch.randn(n_train, seq_len, feat_dim)  # Reconstruction target = self
        x_val = torch.randn(n_val, seq_len, feat_dim)
        y_val = torch.randn(n_val, seq_len, feat_dim)
        x_test = torch.randn(n_test, seq_len, feat_dim)
        y_test = torch.randn(n_test, seq_len, feat_dim)
    elif data_type == "graph":
        # Node features: (batch, num_services, node_feat_dim)
        from nn_pipeline.config import NUM_SERVICES

        n_feat = gat_config.node_feature_dim
        x_train = torch.randn(n_train, NUM_SERVICES, n_feat)
        y_train = torch.randint(0, gat_config.num_fault_classes, (n_train,))
        x_val = torch.randn(n_val, NUM_SERVICES, n_feat)
        y_val = torch.randint(0, gat_config.num_fault_classes, (n_val,))
        x_test = torch.randn(n_test, NUM_SERVICES, n_feat)
        y_test = torch.randint(0, gat_config.num_fault_classes, (n_test,))
    elif data_type == "contrastive":
        # Pairs: (anchor, positive)
        emb_dim = contrastive_config.embedding_dim
        x_train = torch.randn(n_train, 2, emb_dim)
        y_train = torch.zeros(n_train)  # Unused in contrastive
        x_val = torch.randn(n_val, 2, emb_dim)
        y_val = torch.zeros(n_val)
        x_test = torch.randn(n_test, 2, emb_dim)
        y_test = torch.zeros(n_test)
    else:
        raise ValueError(f"Unknown data_type: {data_type}")

    def _make_loader(x: torch.Tensor, y: torch.Tensor) -> DataLoader:
        return DataLoader(
            TensorDataset(x, y),
            batch_size=bs,
            shuffle=True,
            num_workers=0,
            pin_memory=False,
        )

    return _make_loader(x_train, y_train), _make_loader(x_val, y_val), _make_loader(x_test, y_test)


# ─────────────────────────────────────────────────────────────────────
# Model factories (lazy-import with fallback stubs)
# ─────────────────────────────────────────────────────────────────────


def _build_contrastive_model() -> nn.Module:
    """Build Log2Vec contrastive learning model."""
    try:
        from nn_pipeline.embeddings.log2vec import Log2VecEncoder

        return Log2VecEncoder(contrastive_config)
    except ImportError:
        logger.warning("Log2VecEncoder not available — using stub.")
        return _StubModel(contrastive_config.embedding_dim, contrastive_config.projection_dim)


def _build_transformer_model() -> nn.Module:
    """Build Transformer classifier."""
    try:
        from nn_pipeline.models.transformer_classifier import TransformerClassifier

        return TransformerClassifier(transformer_config)
    except ImportError:
        logger.warning("TransformerClassifier not available — using stub.")
        return _StubClassifier(
            data_config.per_entry_feature_dim,
            transformer_config.num_fault_classes,
            transformer_config.d_model,
        )


def _build_lstm_ae_model() -> nn.Module:
    """Build LSTM Autoencoder."""
    try:
        from nn_pipeline.models.lstm_autoencoder import LSTMAutoencoder

        return LSTMAutoencoder(lstm_ae_config)
    except ImportError:
        logger.warning("LSTMAutoencoder not available — using stub.")
        return _StubAutoencoder(lstm_ae_config.input_dim, lstm_ae_config.hidden_dim)


def _build_gat_model() -> nn.Module:
    """Build Graph Attention Network."""
    try:
        from nn_pipeline.models.gat import GATModel

        return GATModel(gat_config)
    except ImportError:
        logger.warning("GATModel not available — using stub.")
        return _StubClassifier(
            gat_config.node_feature_dim,
            gat_config.num_fault_classes,
            gat_config.hidden_dim,
        )


def _build_vae_model() -> nn.Module:
    """Build Variational Autoencoder."""
    try:
        from nn_pipeline.models.vae import ConditionalVAE

        return ConditionalVAE(vae_config)
    except ImportError:
        logger.warning("ConditionalVAE not available — using stub.")
        return _StubAutoencoder(vae_config.input_dim, vae_config.hidden_dim)


def _build_distilled_model() -> nn.Module:
    """Build knowledge-distilled GRU student."""
    try:
        from nn_pipeline.models.distilled_gru import DistilledGRU

        return DistilledGRU(distilled_config)
    except ImportError:
        logger.warning("DistilledGRU not available — using stub.")
        return _StubClassifier(
            distilled_config.input_dim,
            distilled_config.num_fault_classes,
            distilled_config.hidden_dim,
        )


# ─────────────────────────────────────────────────────────────────────
# Stub models (for import-safety before real models are built)
# ─────────────────────────────────────────────────────────────────────


class _StubModel(nn.Module):
    """Tiny feedforward stub for testing the training loop."""

    def __init__(self, in_dim: int, out_dim: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, 64),
            nn.ReLU(),
            nn.Linear(64, out_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if x.dim() > 2:
            x = x.mean(dim=1)
        return self.net(x)


class _StubClassifier(nn.Module):
    """Stub classifier that pools sequence input and predicts classes."""

    def __init__(self, feat_dim: int, num_classes: int, hidden: int = 64) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(feat_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, num_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if x.dim() == 3:
            x = x.mean(dim=1)
        return self.net(x)


class _StubAutoencoder(nn.Module):
    """Stub autoencoder for reconstruction losses."""

    def __init__(self, in_dim: int, hidden: int = 64) -> None:
        super().__init__()
        self.encoder = nn.Linear(in_dim, hidden)
        self.decoder = nn.Linear(hidden, in_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        orig_shape = x.shape
        if x.dim() == 3:
            b, s, f = x.shape
            x = x.reshape(b * s, f)
        z = torch.relu(self.encoder(x))
        out = self.decoder(z)
        if len(orig_shape) == 3:
            out = out.reshape(orig_shape)
        return out


# ─────────────────────────────────────────────────────────────────────
# Custom step functions per model type
# ─────────────────────────────────────────────────────────────────────


def _contrastive_train_step(
    model: nn.Module,
    batch: tuple,
    loss_fn: nn.Module,
    device: torch.device,
) -> Tuple[torch.Tensor, Dict[str, float]]:
    """Contrastive training step: batch contains (anchor_positive_pairs, _)."""
    pairs, _ = batch
    pairs = pairs.to(device)
    anchor = pairs[:, 0, :]
    positive = pairs[:, 1, :]

    z_anchor = model(anchor)
    z_positive = model(positive)

    loss = loss_fn(z_anchor, z_positive)
    return loss, {"loss": loss.item()}


def _autoencoder_train_step(
    model: nn.Module,
    batch: tuple,
    loss_fn: nn.Module,
    device: torch.device,
) -> Tuple[torch.Tensor, Dict[str, float]]:
    """Autoencoder training step: reconstruct input."""
    x, _ = batch
    x = x.to(device)
    x_recon = model(x)
    loss = loss_fn(x_recon, x)
    return loss, {"loss": loss.item(), "recon_loss": loss.item()}


# ─────────────────────────────────────────────────────────────────────
# Training orchestration functions
# ─────────────────────────────────────────────────────────────────────


MODEL_ORDER: List[str] = [
    "contrastive",
    "transformer",
    "lstm_ae",
    "gat",
    "vae",
    "distilled",
]


def train_contrastive(device: torch.device, cfg: TrainingConfig) -> nn.Module:
    """Train Log2Vec contrastive encoder."""
    logger.info("=" * 60)
    logger.info("Phase 1: Log2Vec Contrastive Pre-training")
    logger.info("=" * 60)

    model = _build_contrastive_model()
    train_loader, val_loader, _ = _load_data("contrastive", device)

    optimizer = torch.optim.Adam(
        model.parameters(), lr=cfg.contrastive_lr, weight_decay=cfg.weight_decay
    )

    # NT-Xent style loss (cosine similarity)
    loss_fn = nn.CosineEmbeddingLoss()

    trainer = Trainer(
        model=model,
        optimizer=optimizer,
        loss_fn=loss_fn,
        model_name="log2vec_contrastive",
        config=cfg,
        device=device,
        train_step_fn=_contrastive_train_step,
        val_step_fn=_contrastive_train_step,
    )
    trainer.fit(train_loader, val_loader, num_epochs=cfg.contrastive_epochs)
    trainer.export_history()
    return model


def train_transformer(device: torch.device, cfg: TrainingConfig) -> nn.Module:
    """Train Transformer classifier."""
    logger.info("=" * 60)
    logger.info("Phase 2: Transformer Classifier")
    logger.info("=" * 60)

    model = _build_transformer_model()
    train_loader, val_loader, _ = _load_data("classification", device)

    optimizer = torch.optim.AdamW(
        model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
    )
    loss_fn = nn.CrossEntropyLoss()

    trainer = Trainer(
        model=model,
        optimizer=optimizer,
        loss_fn=loss_fn,
        model_name="transformer_classifier",
        config=cfg,
        device=device,
    )
    trainer.fit(train_loader, val_loader)
    trainer.export_history()
    return model


def train_lstm_ae(device: torch.device, cfg: TrainingConfig) -> nn.Module:
    """Train LSTM Autoencoder."""
    logger.info("=" * 60)
    logger.info("Phase 3: LSTM Autoencoder (Anomaly Detection)")
    logger.info("=" * 60)

    model = _build_lstm_ae_model()
    train_loader, val_loader, _ = _load_data("anomaly", device)

    optimizer = torch.optim.Adam(
        model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
    )
    loss_fn = nn.MSELoss()

    trainer = Trainer(
        model=model,
        optimizer=optimizer,
        loss_fn=loss_fn,
        model_name="lstm_autoencoder",
        config=cfg,
        device=device,
        train_step_fn=_autoencoder_train_step,
        val_step_fn=_autoencoder_train_step,
        early_stop_metric="val_loss",
    )
    trainer.fit(train_loader, val_loader)
    trainer.export_history()
    return model


def train_gat(device: torch.device, cfg: TrainingConfig) -> nn.Module:
    """Train Graph Attention Network."""
    logger.info("=" * 60)
    logger.info("Phase 4: Graph Attention Network (RCA)")
    logger.info("=" * 60)

    model = _build_gat_model()
    train_loader, val_loader, _ = _load_data("graph", device)

    optimizer = torch.optim.Adam(
        model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
    )
    loss_fn = nn.CrossEntropyLoss()

    trainer = Trainer(
        model=model,
        optimizer=optimizer,
        loss_fn=loss_fn,
        model_name="gat_rca",
        config=cfg,
        device=device,
    )
    trainer.fit(train_loader, val_loader)
    trainer.export_history()
    return model


def train_vae(device: torch.device, cfg: TrainingConfig) -> nn.Module:
    """Train Variational Autoencoder."""
    logger.info("=" * 60)
    logger.info("Phase 5: VAE (Anomaly Scoring)")
    logger.info("=" * 60)

    model = _build_vae_model()
    train_loader, val_loader, _ = _load_data("anomaly", device)

    optimizer = torch.optim.Adam(
        model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
    )
    loss_fn = nn.MSELoss()

    trainer = Trainer(
        model=model,
        optimizer=optimizer,
        loss_fn=loss_fn,
        model_name="vae_anomaly",
        config=cfg,
        device=device,
        train_step_fn=_autoencoder_train_step,
        val_step_fn=_autoencoder_train_step,
    )
    trainer.fit(train_loader, val_loader)
    trainer.export_history()
    return model


def train_distilled(
    device: torch.device,
    cfg: TrainingConfig,
    teacher_model: Optional[nn.Module] = None,
) -> nn.Module:
    """Train knowledge-distilled GRU student.

    Args:
        device: Target device.
        cfg: Training configuration.
        teacher_model: Trained Transformer teacher for soft label generation.

    Returns:
        Trained student GRU model.
    """
    logger.info("=" * 60)
    logger.info("Phase 6: Knowledge Distillation (GRU Student)")
    logger.info("=" * 60)

    student = _build_distilled_model()
    train_loader, val_loader, _ = _load_data("classification", device)

    # Load teacher if not provided
    if teacher_model is None:
        teacher_model = _build_transformer_model()
        ckpt_mgr = CheckpointManager(MODEL_CHECKPOINTS_DIR, "transformer_classifier")
        if ckpt_mgr.best_checkpoint_exists():
            ckpt_mgr.load(teacher_model, device=device, load_best=True)
            logger.info("Loaded teacher from best checkpoint.")
        else:
            logger.warning("No teacher checkpoint found — distillation uses random teacher.")

    teacher_model = teacher_model.to(device)
    teacher_model.eval()

    # Distillation training step
    temp = distilled_config.temperature
    alpha = distilled_config.alpha

    def _distil_step(
        model: nn.Module,
        batch: tuple,
        loss_fn: nn.Module,
        dev: torch.device,
    ) -> Tuple[torch.Tensor, Dict[str, float]]:
        inputs, targets = batch
        inputs = inputs.to(dev)
        targets = targets.to(dev)

        student_logits = model(inputs)

        with torch.no_grad():
            teacher_logits = teacher_model(inputs)

        # Hard-label loss
        hard_loss = loss_fn(student_logits, targets)

        # Soft-label loss (KL divergence)
        soft_student = torch.nn.functional.log_softmax(student_logits / temp, dim=-1)
        soft_teacher = torch.nn.functional.softmax(teacher_logits / temp, dim=-1)
        kd_loss = torch.nn.functional.kl_div(
            soft_student, soft_teacher, reduction="batchmean"
        ) * (temp * temp)

        loss = alpha * hard_loss + (1.0 - alpha) * kd_loss
        return loss, {
            "loss": loss.item(),
            "hard_loss": hard_loss.item(),
            "kd_loss": kd_loss.item(),
        }

    optimizer = torch.optim.Adam(
        student.parameters(), lr=cfg.distillation_lr, weight_decay=cfg.weight_decay
    )
    loss_fn = nn.CrossEntropyLoss()

    trainer = Trainer(
        model=student,
        optimizer=optimizer,
        loss_fn=loss_fn,
        model_name="distilled_gru",
        config=cfg,
        device=device,
        train_step_fn=_distil_step,
        val_step_fn=_distil_step,
    )
    trainer.fit(train_loader, val_loader, num_epochs=cfg.distillation_epochs)
    trainer.export_history()
    return student


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────

TRAIN_REGISTRY: Dict[str, callable] = {
    "contrastive": lambda dev, cfg, **kw: train_contrastive(dev, cfg),
    "transformer": lambda dev, cfg, **kw: train_transformer(dev, cfg),
    "lstm_ae": lambda dev, cfg, **kw: train_lstm_ae(dev, cfg),
    "gat": lambda dev, cfg, **kw: train_gat(dev, cfg),
    "vae": lambda dev, cfg, **kw: train_vae(dev, cfg),
    "distilled": lambda dev, cfg, **kw: train_distilled(dev, cfg, kw.get("teacher")),
}


def train_all(
    models: Optional[List[str]] = None,
    skip: Optional[List[str]] = None,
) -> None:
    """Run training for all (or selected) models in order.

    Args:
        models: Explicit list of model names to train. If None, trains all.
        skip: List of model names to skip.
    """
    seed_everything(training_config.seed)
    device = get_device(training_config)

    to_train = models or MODEL_ORDER
    skip_set = set(skip or [])
    to_train = [m for m in to_train if m not in skip_set]

    logger.info("Training plan: %s", to_train)

    trained: Dict[str, nn.Module] = {}

    for name in to_train:
        if name not in TRAIN_REGISTRY:
            logger.error("Unknown model: %s — skipping.", name)
            continue

        t0 = time.time()
        try:
            kwargs = {}
            if name == "distilled" and "transformer" in trained:
                kwargs["teacher"] = trained["transformer"]

            model = TRAIN_REGISTRY[name](device, training_config, **kwargs)
            trained[name] = model
            elapsed = time.time() - t0
            logger.info(
                "✓ %s training complete (%.1f s)", name, elapsed
            )
        except Exception as e:
            logger.error("✗ %s training failed: %s", name, e, exc_info=True)

    logger.info("=" * 60)
    logger.info("All training complete. Models trained: %s", list(trained.keys()))
    logger.info("=" * 60)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Train all Neural Network Log Analyzer models."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=MODEL_ORDER,
        default=None,
        help="Specific models to train (default: all, in order).",
    )
    parser.add_argument(
        "--skip",
        nargs="+",
        choices=MODEL_ORDER,
        default=None,
        help="Models to skip.",
    )
    args = parser.parse_args()
    train_all(models=args.models, skip=args.skip)


if __name__ == "__main__":
    main()

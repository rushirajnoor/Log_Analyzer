"""
Central configuration for the Neural Network Log Analyzer pipeline.

All hyperparameters, paths, model configs, and constants live here.
Every module imports from this file to stay synchronized.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional

# ─────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
NN_PIPELINE_ROOT = Path(__file__).resolve().parent

RAW_LOGS_PATH = PROJECT_ROOT / "raw_logs.txt"
PROCESSED_DATA_DIR = NN_PIPELINE_ROOT / "processed_data"
MODEL_CHECKPOINTS_DIR = NN_PIPELINE_ROOT / "checkpoints"
TENSORBOARD_LOG_DIR = NN_PIPELINE_ROOT / "runs"
TEMPLATE_CACHE_DIR = NN_PIPELINE_ROOT / "template_cache"

# Ensure directories exist
for _dir in [PROCESSED_DATA_DIR, MODEL_CHECKPOINTS_DIR, TENSORBOARD_LOG_DIR, TEMPLATE_CACHE_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────
# Service definitions (Online Boutique microservices)
# ─────────────────────────────────────────────────────────────────────
SERVICES = [
    "adservice",
    "cartservice",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "loadgenerator",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
    "redis-cart",
    "shippingservice",
]

SERVICE_TO_IDX: Dict[str, int] = {s: i for i, s in enumerate(SERVICES)}
IDX_TO_SERVICE: Dict[int, str] = {i: s for i, s in enumerate(SERVICES)}
NUM_SERVICES = len(SERVICES)

# Service dependency graph (directed: parent → [children])
# Represents "parent depends on children"
SERVICE_DEPENDENCIES = {
    "frontend": ["cartservice", "productcatalogservice", "recommendationservice", "currencyservice", "adservice", "checkoutservice", "shippingservice"],
    "cartservice": ["redis-cart"],
    "checkoutservice": ["paymentservice", "shippingservice", "emailservice", "cartservice", "currencyservice", "productcatalogservice"],
    "recommendationservice": ["productcatalogservice"],
    # Leaf services (no dependencies)
    "adservice": [],
    "currencyservice": [],
    "emailservice": [],
    "paymentservice": [],
    "productcatalogservice": [],
    "redis-cart": [],
    "shippingservice": [],
    "loadgenerator": ["frontend"],
}

# ─────────────────────────────────────────────────────────────────────
# Log levels
# ─────────────────────────────────────────────────────────────────────
LOG_LEVELS = ["INFO", "WARNING", "ERROR"]
LEVEL_TO_IDX: Dict[str, int] = {l: i for i, l in enumerate(LOG_LEVELS)}
IDX_TO_LEVEL: Dict[int, str] = {i: l for i, l in enumerate(LOG_LEVELS)}
NUM_LEVELS = len(LOG_LEVELS)

# Fault types
FAULT_TYPES = [
    "normal",            # No fault
    "service_failure",   # Service crashed / unresponsive
    "dependency_failure",# Upstream/downstream dependency issue
    "resource_failure",  # OOM, CPU exhaustion
    "network_failure",   # Connection refused, timeout
    "unknown_fault",     # Unclassifiable
]
FAULT_TO_IDX: Dict[str, int] = {f: i for i, f in enumerate(FAULT_TYPES)}
IDX_TO_FAULT: Dict[int, str] = {i: f for i, f in enumerate(FAULT_TYPES)}
NUM_FAULT_TYPES = len(FAULT_TYPES)

# Error keywords for feature engineering
ERROR_KEYWORDS = [
    "error", "fail", "timeout", "refused", "unavailable",
    "connection", "oom", "crash", "exception", "panic",
    "deadlock", "retry", "backoff", "reject", "abort",
]

# ─────────────────────────────────────────────────────────────────────
# Data Pipeline Config
# ─────────────────────────────────────────────────────────────────────

@dataclass
class DataConfig:
    """Configuration for data pipeline."""
    # Log parsing
    max_message_length: int = 512        # Max chars in a log message
    min_message_length: int = 5          # Skip very short messages

    # Template extraction (Drain3)
    drain_depth: int = 4                 # Drain tree depth
    drain_sim_th: float = 0.4            # Similarity threshold
    drain_max_children: int = 100        # Max children per node
    max_templates: int = 500             # Cap template vocabulary

    # Windowing
    window_size: int = 64                # Number of log entries per window
    window_stride: int = 32              # Stride between windows
    time_window_seconds: float = 30.0    # Time-based window duration
    time_window_stride_seconds: float = 15.0

    # Feature dimensions
    per_entry_feature_dim: int = 20      # Features per log entry
    num_error_keywords: int = len(ERROR_KEYWORDS)

    # Train/val/test split
    train_ratio: float = 0.7
    val_ratio: float = 0.15
    test_ratio: float = 0.15

    # Data loading
    batch_size: int = 64
    num_workers: int = 4
    pin_memory: bool = True


# ─────────────────────────────────────────────────────────────────────
# Model Configs
# ─────────────────────────────────────────────────────────────────────

@dataclass
class TransformerConfig:
    """Transformer Encoder for log classification."""
    d_model: int = 128                   # Embedding dimension
    nhead: int = 8                       # Attention heads
    num_layers: int = 4                  # Encoder layers
    dim_feedforward: int = 512           # FFN hidden dim
    dropout: float = 0.1
    max_seq_len: int = 64               # Same as window_size
    num_severity_classes: int = NUM_LEVELS
    num_fault_classes: int = NUM_FAULT_TYPES
    use_cls_token: bool = True           # Prepend [CLS] for classification


@dataclass
class LSTMAutoencoderConfig:
    """BiLSTM Autoencoder for anomaly detection."""
    input_dim: int = 20                  # Per-entry feature dim
    hidden_dim: int = 64                 # LSTM hidden dim
    bottleneck_dim: int = 32             # Compressed representation
    num_layers: int = 2                  # LSTM layers
    bidirectional: bool = True
    dropout: float = 0.1
    seq_len: int = 64                    # Same as window_size
    anomaly_percentile: float = 95.0     # Threshold percentile on training data


@dataclass
class GATConfig:
    """Graph Attention Network for RCA."""
    node_feature_dim: int = 128          # Per-service feature vector
    hidden_dim: int = 64
    output_dim: int = 32
    num_heads_layer1: int = 4
    num_heads_layer2: int = 4
    num_heads_layer3: int = 2
    dropout: float = 0.1
    num_services: int = NUM_SERVICES
    num_fault_classes: int = NUM_FAULT_TYPES
    learn_edges: bool = True             # Learn additional edges beyond known deps
    edge_feature_dim: int = 8            # Features on edges (call rate, error rate, etc.)


@dataclass
class VAEConfig:
    """Variational Autoencoder for anomaly scoring."""
    input_dim: int = 32                  # log features (20) + service one-hot (12)
    hidden_dim: int = 64
    latent_dim: int = 16
    condition_dim: int = NUM_SERVICES    # Conditional on service
    beta: float = 1.0                    # KL weight (β-VAE)
    dropout: float = 0.1


@dataclass
class ContrastiveConfig:
    """Contrastive learning (Log2Vec) for log embeddings."""
    embedding_dim: int = 128             # Final embedding dimension
    projection_dim: int = 32             # Projection head output
    encoder_layers: int = 2              # Transformer layers in encoder
    encoder_heads: int = 4
    temperature: float = 0.07           # NT-Xent temperature
    token_dropout_rate: float = 0.15     # Augmentation dropout
    max_tokens_per_template: int = 64    # Max tokens in a log template


@dataclass
class DistilledModelConfig:
    """Knowledge-distilled GRU for fast inference."""
    input_dim: int = 128                 # Embedding dim (from Log2Vec)
    hidden_dim: int = 64
    num_layers: int = 2
    dropout: float = 0.1
    num_severity_classes: int = NUM_LEVELS
    num_fault_classes: int = NUM_FAULT_TYPES
    temperature: float = 4.0            # Distillation temperature
    alpha: float = 0.3                  # Weight for hard labels vs soft labels


# ─────────────────────────────────────────────────────────────────────
# Training Config
# ─────────────────────────────────────────────────────────────────────

@dataclass
class TrainingConfig:
    """Training hyperparameters."""
    # General
    device: str = "cuda"                 # "cuda" or "cpu"
    seed: int = 42
    num_epochs: int = 50
    early_stopping_patience: int = 10
    gradient_clip_norm: float = 1.0

    # Optimizer
    learning_rate: float = 1e-3
    weight_decay: float = 1e-5
    scheduler: str = "cosine"            # "cosine" | "step" | "plateau"
    warmup_steps: int = 100

    # Logging
    log_every_n_steps: int = 10
    val_every_n_epochs: int = 1
    save_every_n_epochs: int = 5

    # Contrastive pre-training
    contrastive_epochs: int = 30
    contrastive_lr: float = 5e-4

    # Distillation
    distillation_epochs: int = 20
    distillation_lr: float = 1e-3


# ─────────────────────────────────────────────────────────────────────
# Ensemble Config
# ─────────────────────────────────────────────────────────────────────

@dataclass
class EnsembleConfig:
    """Ensemble orchestrator configuration."""
    # Model weights (learned or fixed)
    transformer_weight: float = 0.35
    lstm_ae_weight: float = 0.25
    gat_weight: float = 0.25
    vae_weight: float = 0.15

    # Thresholds
    anomaly_threshold: float = 0.5       # Combined anomaly score threshold
    high_confidence_threshold: float = 0.8
    medium_confidence_threshold: float = 0.5
    low_confidence_threshold: float = 0.3

    # Routing
    skip_gat_if_no_anomaly: bool = True  # Only run GAT if anomaly detected


# ─────────────────────────────────────────────────────────────────────
# Database config (shared with existing pipeline)
# ─────────────────────────────────────────────────────────────────────
DATABASE_URL = "postgresql://loguser:password@localhost:5432/logdb"


# ─────────────────────────────────────────────────────────────────────
# Convenience: default config instances
# ─────────────────────────────────────────────────────────────────────
data_config = DataConfig()
transformer_config = TransformerConfig()
lstm_ae_config = LSTMAutoencoderConfig()
gat_config = GATConfig()
vae_config = VAEConfig()
contrastive_config = ContrastiveConfig()
distilled_config = DistilledModelConfig()
training_config = TrainingConfig()
ensemble_config = EnsembleConfig()

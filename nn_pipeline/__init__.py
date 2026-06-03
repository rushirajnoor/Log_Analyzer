"""
Neural Network Log Analyzer Pipeline.

A modular multi-model deep learning system for log analysis,
anomaly detection, and root cause analysis — replacing LLM-based approaches
with custom neural network architectures.

Architecture:
    - Transformer Encoder: Log classification + severity scoring
    - BiLSTM-Autoencoder: Unsupervised anomaly detection
    - Graph Attention Network (GAT): Service dependency RCA
    - VAE: Probabilistic anomaly scoring
    - Contrastive Learning (Log2Vec): Self-supervised log embeddings
    - Knowledge Distillation: Lightweight inference model
    - Ensemble Orchestrator: Confidence-weighted final predictions
"""

__version__ = "0.1.0"

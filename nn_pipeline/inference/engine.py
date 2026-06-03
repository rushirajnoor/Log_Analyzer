"""
Real-time inference engine — the main entry point replacing llm_rca.py.

``NNInferenceEngine`` loads all trained model checkpoints, preprocesses
raw log text or LogEntry sequences, runs the ensemble, and returns
structured predictions with confidence scores and explanations.

Supports both **batch** (historical analysis) and **streaming** (live
monitoring) modes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch
import torch.nn.functional as F

from nn_pipeline.config import (
    MODEL_CHECKPOINTS_DIR,
    IDX_TO_FAULT,
    IDX_TO_LEVEL,
    IDX_TO_SERVICE,
    NUM_SERVICES,
    SERVICES,
    data_config,
    ensemble_config,
    training_config,
    transformer_config,
    lstm_ae_config,
    gat_config,
    vae_config,
    distilled_config,
)

logger = logging.getLogger(__name__)


@dataclass
class Prediction:
    """Structured prediction output from the inference engine.

    Attributes:
        is_anomaly: Whether an anomaly was detected.
        anomaly_score: Combined anomaly score [0.0, 1.0].
        severity: Predicted severity class ("INFO", "WARNING", "ERROR").
        severity_score: Continuous severity [0.0, 1.0].
        fault_type: Predicted fault type string.
        root_cause_service: Predicted root cause service name.
        root_cause_confidence: Confidence in root cause prediction [0.0, 1.0].
        confidence: Overall prediction confidence ("HIGH", "MEDIUM", "LOW").
        explanation: Human-readable explanation string.
        model_scores: Per-model breakdown of scores.
        attention_data: Raw attention weights for visualization (optional).
        inference_time_ms: Time taken for inference in milliseconds.
    """
    is_anomaly: bool = False
    anomaly_score: float = 0.0
    severity: str = "INFO"
    severity_score: float = 0.0
    fault_type: str = "normal"
    root_cause_service: str = "unknown"
    root_cause_confidence: float = 0.0
    confidence: str = "LOW"
    explanation: str = ""
    model_scores: Dict[str, float] = field(default_factory=dict)
    attention_data: Optional[Dict] = None
    inference_time_ms: float = 0.0


class NNInferenceEngine:
    """Neural Network inference engine replacing the LLM-based RCA.

    Usage::

        engine = NNInferenceEngine()
        engine.load_models()

        # From raw log entries
        prediction = engine.predict(log_entries)

        # From raw text (auto-parses)
        prediction = engine.predict_from_text(raw_log_lines)
    """

    def __init__(
        self,
        checkpoint_dir: Optional[Path] = None,
        device: Optional[str] = None,
        use_distilled: bool = False,
    ) -> None:
        """Initialize the inference engine.

        Args:
            checkpoint_dir: Directory containing model checkpoints.
            device: "cuda" or "cpu". Auto-detects if None.
            use_distilled: If True, use the distilled GRU instead of Transformer.
        """
        self.checkpoint_dir = checkpoint_dir or MODEL_CHECKPOINTS_DIR
        self.use_distilled = use_distilled

        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device)

        self.models_loaded = False
        self.transformer = None
        self.lstm_ae = None
        self.gat = None
        self.vae = None
        self.distilled = None
        self.svc_aggregator = None
        self.template_extractor = None

        # Anomaly threshold (computed from training data)
        self.lstm_ae_threshold = 0.5
        self.vae_threshold = 0.5

        logger.info("NNInferenceEngine initialized (device=%s)", self.device)

    def load_models(self) -> bool:
        """Load all model checkpoints from disk.

        Returns:
            True if at least the core models loaded successfully.
        """
        try:
            from nn_pipeline.models.transformer_classifier import LogTransformerClassifier
            from nn_pipeline.models.lstm_autoencoder import LSTMAutoencoder
            from nn_pipeline.models.gat_rca import GATRCA, ServiceFeatureAggregator
            from nn_pipeline.models.vae_anomaly import ConditionalVAE
            from nn_pipeline.models.distilled_model import DistilledGRU
            from nn_pipeline.data.feature_engineer import ENTRY_FEATURE_DIM
        except ImportError as e:
            logger.error("Failed to import model modules: %s", e)
            return False

        loaded_count = 0

        # Load Transformer
        transformer_path = self.checkpoint_dir / "transformer_best.pt"
        if transformer_path.exists():
            self.transformer = LogTransformerClassifier()
            self.transformer.load_state_dict(torch.load(transformer_path, map_location=self.device))
            self.transformer.to(self.device).eval()
            loaded_count += 1
            logger.info("Loaded Transformer classifier")

        # Load LSTM-AE
        lstm_path = self.checkpoint_dir / "lstm_ae_best.pt"
        if lstm_path.exists():
            self.lstm_ae = LSTMAutoencoder()
            self.lstm_ae.load_state_dict(torch.load(lstm_path, map_location=self.device))
            self.lstm_ae.to(self.device).eval()
            loaded_count += 1
            logger.info("Loaded LSTM Autoencoder")

        # Load GAT
        gat_path = self.checkpoint_dir / "gat_best.pt"
        if gat_path.exists():
            self.gat = GATRCA()
            self.gat.load_state_dict(torch.load(gat_path, map_location=self.device))
            self.gat.to(self.device).eval()
            self.svc_aggregator = ServiceFeatureAggregator(
                ENTRY_FEATURE_DIM, gat_config.node_feature_dim
            )
            svc_agg_path = self.checkpoint_dir / "svc_aggregator_best.pt"
            if svc_agg_path.exists():
                self.svc_aggregator.load_state_dict(
                    torch.load(svc_agg_path, map_location=self.device)
                )
            self.svc_aggregator.to(self.device).eval()
            loaded_count += 1
            logger.info("Loaded GAT RCA model")

        # Load VAE
        vae_path = self.checkpoint_dir / "vae_best.pt"
        if vae_path.exists():
            self.vae = ConditionalVAE()
            self.vae.load_state_dict(torch.load(vae_path, map_location=self.device))
            self.vae.to(self.device).eval()
            loaded_count += 1
            logger.info("Loaded Conditional VAE")

        # Load Distilled model
        distilled_path = self.checkpoint_dir / "distilled_best.pt"
        if distilled_path.exists():
            self.distilled = DistilledGRU()
            self.distilled.load_state_dict(torch.load(distilled_path, map_location=self.device))
            self.distilled.to(self.device).eval()
            loaded_count += 1
            logger.info("Loaded Distilled GRU")

        # Load thresholds
        thresh_path = self.checkpoint_dir / "thresholds.pt"
        if thresh_path.exists():
            thresholds = torch.load(thresh_path, map_location="cpu")
            self.lstm_ae_threshold = thresholds.get("lstm_ae", 0.5)
            self.vae_threshold = thresholds.get("vae", 0.5)

        # Load template extractor
        try:
            from nn_pipeline.data.template_extractor import TemplateExtractor
            self.template_extractor = TemplateExtractor()
            self.template_extractor.load()
        except Exception:
            logger.warning("Could not load template extractor")

        self.models_loaded = loaded_count > 0
        logger.info("Loaded %d/%d models", loaded_count, 5)
        return self.models_loaded

    @torch.no_grad()
    def predict(
        self,
        entry_features: torch.Tensor,
        service_indices: torch.Tensor,
        window_features: Optional[torch.Tensor] = None,
    ) -> Prediction:
        """Run ensemble inference on preprocessed features.

        Args:
            entry_features: Shape ``(seq_len, feature_dim)`` or ``(1, seq_len, feature_dim)``.
            service_indices: Shape ``(seq_len,)`` or ``(1, seq_len)``.
            window_features: Shape ``(window_dim,)`` (optional).

        Returns:
            Prediction dataclass with all results.
        """
        start_time = time.perf_counter()

        # Ensure batch dimension
        if entry_features.dim() == 2:
            entry_features = entry_features.unsqueeze(0)  # (1, seq, feat)
        if service_indices.dim() == 1:
            service_indices = service_indices.unsqueeze(0)

        entry_features = entry_features.to(self.device)
        service_indices = service_indices.to(self.device)

        scores: Dict[str, float] = {}
        attention_data = {}

        # ── 1. Anomaly Detection (always runs) ──
        anomaly_score = 0.0

        # LSTM-AE anomaly
        if self.lstm_ae is not None:
            lstm_out = self.lstm_ae(entry_features)
            lstm_score = lstm_out["reconstruction_error"].mean().item()
            lstm_anomaly = float(lstm_score > self.lstm_ae_threshold)
            scores["lstm_ae"] = lstm_score
            anomaly_score += ensemble_config.lstm_ae_weight * lstm_anomaly

        # VAE anomaly (on aggregated features)
        if self.vae is not None:
            # Aggregate to single feature vector per window
            feat_mean = entry_features.squeeze(0).mean(dim=0)[:self.vae.feature_dim]
            svc_mode = service_indices.squeeze(0).mode().values.item()
            svc_onehot = torch.zeros(1, NUM_SERVICES, device=self.device)
            svc_onehot[0, svc_mode] = 1.0
            vae_out = self.vae(feat_mean.unsqueeze(0), svc_onehot)
            vae_score = vae_out["anomaly_score"].item()
            scores["vae"] = vae_score
            vae_anomaly = float(vae_score > self.vae_threshold)
            anomaly_score += ensemble_config.vae_weight * vae_anomaly

        # ── 2. Classification (if anomaly or always for completeness) ──
        severity = "INFO"
        severity_score_val = 0.0
        fault_type = "normal"
        fault_logits = None

        classifier = self.distilled if (self.use_distilled and self.distilled) else self.transformer

        if classifier is not None:
            cls_out = classifier(entry_features)
            sev_logits = cls_out["severity_logits"]  # (1, num_severity)
            fault_logits_t = cls_out["fault_logits"]  # (1, num_fault)
            sev_score = cls_out["severity_score"]    # (1,)

            sev_pred = sev_logits.argmax(dim=-1).item()
            fault_pred = fault_logits_t.argmax(dim=-1).item()

            severity = IDX_TO_LEVEL.get(sev_pred, "INFO")
            fault_type = IDX_TO_FAULT.get(fault_pred, "normal")
            severity_score_val = sev_score.item()
            fault_logits = fault_logits_t

            sev_conf = F.softmax(sev_logits, dim=-1).max().item()
            scores["transformer"] = sev_conf
            anomaly_score += ensemble_config.transformer_weight * (1.0 - sev_conf if severity != "INFO" else 0.0)

            # Extract attention weights if available
            if hasattr(cls_out, "get") and "attention_weights" in cls_out:
                attention_data["transformer"] = cls_out["attention_weights"]

        # ── 3. Root Cause Analysis (GAT) ──
        root_cause_service = "unknown"
        root_cause_conf = 0.0

        if self.gat is not None and self.svc_aggregator is not None:
            node_feats = self.svc_aggregator(
                entry_features.squeeze(0),
                service_indices.squeeze(0),
            )
            gat_out = self.gat(node_feats)
            rc_logits = gat_out["root_cause_logits"]  # (num_services,)
            rc_probs = F.softmax(rc_logits, dim=-1)
            rc_pred = rc_probs.argmax().item()
            root_cause_service = IDX_TO_SERVICE.get(rc_pred, "unknown")
            root_cause_conf = rc_probs[rc_pred].item()
            scores["gat"] = root_cause_conf

            risk = gat_out["risk_score"].item()
            anomaly_score += ensemble_config.gat_weight * risk

            attention_data["gat"] = {
                "edge_attention": {
                    k: v.cpu().numpy().tolist() if isinstance(v, torch.Tensor) else v
                    for k, v in gat_out.get("attention_weights", {}).items()
                }
            }

        # ── 4. Combine and determine confidence ──
        anomaly_score = min(anomaly_score, 1.0)
        is_anomaly = anomaly_score > ensemble_config.anomaly_threshold

        if anomaly_score >= ensemble_config.high_confidence_threshold:
            confidence = "HIGH"
        elif anomaly_score >= ensemble_config.medium_confidence_threshold:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        # ── 5. Generate explanation ──
        explanation_parts = []
        if is_anomaly:
            explanation_parts.append(
                f"Anomaly detected (score={anomaly_score:.2f}, confidence={confidence})."
            )
            if fault_type != "normal":
                explanation_parts.append(f"Fault type: {fault_type}.")
            if root_cause_service != "unknown":
                explanation_parts.append(
                    f"Root cause: {root_cause_service} (confidence={root_cause_conf:.2f})."
                )
        else:
            explanation_parts.append("No anomaly detected. System operating normally.")

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return Prediction(
            is_anomaly=is_anomaly,
            anomaly_score=anomaly_score,
            severity=severity,
            severity_score=severity_score_val,
            fault_type=fault_type,
            root_cause_service=root_cause_service,
            root_cause_confidence=root_cause_conf,
            confidence=confidence,
            explanation=" ".join(explanation_parts),
            model_scores=scores,
            attention_data=attention_data if attention_data else None,
            inference_time_ms=elapsed_ms,
        )

    def predict_from_entries(
        self,
        entries,  # List[LogEntry]
    ) -> Prediction:
        """Run inference from parsed LogEntry objects.

        This is the drop-in replacement for ``infer_with_llm()`` in the
        existing pipeline.
        """
        from nn_pipeline.data.feature_engineer import extract_entry_features_batch, ENTRY_FEATURE_DIM
        from nn_pipeline.data.window_builder import pad_or_truncate_window

        # Ensure window size
        padded = pad_or_truncate_window(list(entries), data_config.window_size)

        # Get template IDs
        template_ids = None
        if self.template_extractor:
            template_ids = self.template_extractor.fit_and_transform(
                [e.message for e in padded]
            )

        # Extract features
        features = extract_entry_features_batch(
            padded, template_ids=template_ids,
        )
        service_indices = np.array([max(e.service_idx, 0) for e in padded])

        return self.predict(
            torch.tensor(features, dtype=torch.float32),
            torch.tensor(service_indices, dtype=torch.long),
        )

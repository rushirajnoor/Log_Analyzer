"""
Ensemble orchestrator — routes inputs through models and combines predictions.

Decision flow:
    1. Anomaly detection (LSTM-AE + VAE) → is this anomalous?
    2. If anomaly → classify with Transformer → fault type + severity
    3. If anomaly → run GAT for root cause analysis → service attribution
    4. Ensemble combine via confidence-weighted voting
    5. Final prediction with explanation and confidence level

The orchestrator does NOT load models itself — it receives pre-loaded models
from the inference engine (``nn_pipeline.inference.engine``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn

from nn_pipeline.config import (
    FAULT_TYPES,
    IDX_TO_FAULT,
    IDX_TO_LEVEL,
    IDX_TO_SERVICE,
    LOG_LEVELS,
    NUM_FAULT_TYPES,
    NUM_SERVICES,
    SERVICES,
    EnsembleConfig,
    ensemble_config,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Data classes for structured results
# ─────────────────────────────────────────────────────────────────────


class ConfidenceLevel(str, Enum):
    """Confidence level classification."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass
class AnomalyResult:
    """Output from anomaly detection stage."""

    is_anomaly: bool
    lstm_ae_score: float = 0.0
    vae_score: float = 0.0
    combined_score: float = 0.0
    threshold: float = 0.5


@dataclass
class ClassificationResult:
    """Output from classification stage."""

    fault_type: str = "unknown_fault"
    fault_probs: Dict[str, float] = field(default_factory=dict)
    severity: str = "INFO"
    severity_probs: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class RCAResult:
    """Output from root cause analysis (GAT) stage."""

    root_cause_service: str = "unknown"
    service_scores: Dict[str, float] = field(default_factory=dict)
    edge_attentions: Optional[np.ndarray] = None
    confidence: float = 0.0


@dataclass
class EnsemblePrediction:
    """Complete ensemble prediction with all metadata."""

    # Top-level results
    is_anomaly: bool = False
    fault_type: str = "normal"
    severity: str = "INFO"
    root_cause_service: str = "unknown"
    confidence: ConfidenceLevel = ConfidenceLevel.UNKNOWN
    confidence_score: float = 0.0

    # Individual model outputs
    anomaly: Optional[AnomalyResult] = None
    classification: Optional[ClassificationResult] = None
    rca: Optional[RCAResult] = None

    # Explanation
    explanation: str = ""
    model_contributions: Dict[str, float] = field(default_factory=dict)

    # Timing
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for JSON output."""
        d: Dict[str, Any] = {
            "is_anomaly": self.is_anomaly,
            "fault_type": self.fault_type,
            "severity": self.severity,
            "root_cause_service": self.root_cause_service,
            "confidence": self.confidence.value,
            "confidence_score": round(self.confidence_score, 4),
            "explanation": self.explanation,
            "model_contributions": {
                k: round(v, 4) for k, v in self.model_contributions.items()
            },
            "latency_ms": round(self.latency_ms, 2),
        }
        if self.anomaly is not None:
            d["anomaly_detail"] = {
                "is_anomaly": self.anomaly.is_anomaly,
                "lstm_ae_score": round(self.anomaly.lstm_ae_score, 4),
                "vae_score": round(self.anomaly.vae_score, 4),
                "combined_score": round(self.anomaly.combined_score, 4),
            }
        if self.classification is not None:
            d["classification_detail"] = {
                "fault_type": self.classification.fault_type,
                "fault_probs": {
                    k: round(v, 4) for k, v in self.classification.fault_probs.items()
                },
                "severity": self.classification.severity,
            }
        if self.rca is not None:
            d["rca_detail"] = {
                "root_cause_service": self.rca.root_cause_service,
                "service_scores": {
                    k: round(v, 4) for k, v in self.rca.service_scores.items()
                },
            }
        return d


# ─────────────────────────────────────────────────────────────────────
# Ensemble Orchestrator
# ─────────────────────────────────────────────────────────────────────


class EnsembleOrchestrator:
    """Routes input through all models and produces a unified prediction.

    Models are injected at construction time (loaded by the inference engine).
    Any model can be ``None`` — the orchestrator gracefully degrades.

    Example::

        orchestrator = EnsembleOrchestrator(
            transformer=transformer_model,
            lstm_ae=lstm_ae_model,
            gat=gat_model,
            vae=vae_model,
        )
        prediction = orchestrator.predict(features)
    """

    def __init__(
        self,
        transformer: Optional[nn.Module] = None,
        lstm_ae: Optional[nn.Module] = None,
        gat: Optional[nn.Module] = None,
        vae: Optional[nn.Module] = None,
        distilled: Optional[nn.Module] = None,
        config: Optional[EnsembleConfig] = None,
        device: Optional[torch.device] = None,
        anomaly_threshold: Optional[float] = None,
    ) -> None:
        """
        Args:
            transformer: Trained Transformer classifier (or None).
            lstm_ae: Trained LSTM-AE anomaly detector (or None).
            gat: Trained GAT for RCA (or None).
            vae: Trained VAE anomaly scorer (or None).
            distilled: Trained distilled GRU (optional fast path).
            config: EnsembleConfig instance.
            device: Target device for inference.
            anomaly_threshold: Override anomaly score threshold.
        """
        self.cfg = config or ensemble_config
        self.device = device or torch.device("cpu")

        self.transformer = transformer
        self.lstm_ae = lstm_ae
        self.gat = gat
        self.vae = vae
        self.distilled = distilled

        self.anomaly_threshold = (
            anomaly_threshold
            if anomaly_threshold is not None
            else self.cfg.anomaly_threshold
        )

        # Pre-compute normalized model weights
        self._weights = self._normalize_weights()

        # Set models to eval mode
        for m in [self.transformer, self.lstm_ae, self.gat, self.vae, self.distilled]:
            if m is not None:
                m.eval()

    def _normalize_weights(self) -> Dict[str, float]:
        """Normalize model weights to sum to 1.0 (only for available models)."""
        raw: Dict[str, float] = {}
        if self.transformer is not None:
            raw["transformer"] = self.cfg.transformer_weight
        if self.lstm_ae is not None:
            raw["lstm_ae"] = self.cfg.lstm_ae_weight
        if self.gat is not None:
            raw["gat"] = self.cfg.gat_weight
        if self.vae is not None:
            raw["vae"] = self.cfg.vae_weight

        total = sum(raw.values())
        if total > 0:
            return {k: v / total for k, v in raw.items()}
        return raw

    # ── Stage 1: Anomaly Detection ──────────────────────────────────

    @torch.no_grad()
    def _detect_anomaly(
        self,
        features: torch.Tensor,
        service_idx: Optional[int] = None,
    ) -> AnomalyResult:
        """Run LSTM-AE and VAE for anomaly scoring.

        Args:
            features: Input feature tensor, shape ``(1, seq_len, feat_dim)``.
            service_idx: Service index for conditional VAE (optional).

        Returns:
            AnomalyResult with individual and combined scores.
        """
        lstm_score = 0.0
        vae_score = 0.0

        # LSTM-AE: reconstruction error = anomaly score
        if self.lstm_ae is not None:
            try:
                x = features.to(self.device)
                x_recon = self.lstm_ae(x)
                # Mean squared reconstruction error
                recon_error = torch.mean((x - x_recon) ** 2).item()
                lstm_score = min(recon_error, 1.0)  # Clamp to [0, 1]
            except Exception as e:
                logger.debug("LSTM-AE scoring failed: %s", e)

        # VAE: ELBO-based anomaly scoring
        if self.vae is not None:
            try:
                x = features.to(self.device)
                if x.dim() == 3:
                    x = x.mean(dim=1)  # Pool sequence

                x_recon = self.vae(x)
                recon_error = torch.mean((x - x_recon) ** 2).item()
                vae_score = min(recon_error, 1.0)
            except Exception as e:
                logger.debug("VAE scoring failed: %s", e)

        # Weighted combination
        w_lstm = self._weights.get("lstm_ae", 0.5)
        w_vae = self._weights.get("vae", 0.5)
        denom = w_lstm + w_vae
        if denom > 0:
            combined = (w_lstm * lstm_score + w_vae * vae_score) / denom
        else:
            combined = max(lstm_score, vae_score)

        return AnomalyResult(
            is_anomaly=combined >= self.anomaly_threshold,
            lstm_ae_score=lstm_score,
            vae_score=vae_score,
            combined_score=combined,
            threshold=self.anomaly_threshold,
        )

    # ── Stage 2: Classification ─────────────────────────────────────

    @torch.no_grad()
    def _classify(
        self,
        features: torch.Tensor,
        use_distilled: bool = False,
    ) -> ClassificationResult:
        """Run Transformer (or distilled GRU) for classification.

        Args:
            features: Input features, shape ``(1, seq_len, feat_dim)``.
            use_distilled: If True, use the distilled model for speed.

        Returns:
            ClassificationResult with fault type and severity.
        """
        model = self.distilled if (use_distilled and self.distilled is not None) else self.transformer

        if model is None:
            return ClassificationResult()

        try:
            x = features.to(self.device)
            logits = model(x)

            # Expect logits shape: (batch, num_fault_classes)
            probs = torch.softmax(logits, dim=-1).squeeze(0)
            pred_idx = probs.argmax().item()

            fault_type = IDX_TO_FAULT.get(pred_idx, "unknown_fault")
            fault_probs = {
                IDX_TO_FAULT.get(i, f"class_{i}"): float(probs[i])
                for i in range(len(probs))
            }
            confidence = float(probs[pred_idx])

            return ClassificationResult(
                fault_type=fault_type,
                fault_probs=fault_probs,
                severity=self._infer_severity(fault_type, confidence),
                confidence=confidence,
            )
        except Exception as e:
            logger.debug("Classification failed: %s", e)
            return ClassificationResult()

    def _infer_severity(self, fault_type: str, confidence: float) -> str:
        """Infer severity from fault type and confidence."""
        if fault_type == "normal":
            return "INFO"
        elif fault_type in ("service_failure", "resource_failure"):
            return "ERROR"
        elif confidence > self.cfg.high_confidence_threshold:
            return "ERROR"
        elif confidence > self.cfg.medium_confidence_threshold:
            return "WARNING"
        return "INFO"

    # ── Stage 3: Root Cause Analysis ────────────────────────────────

    @torch.no_grad()
    def _root_cause_analysis(
        self,
        features: torch.Tensor,
    ) -> RCAResult:
        """Run GAT for service-level root cause analysis.

        Args:
            features: Node feature tensor for the service graph.

        Returns:
            RCAResult with root cause service and scores.
        """
        if self.gat is None:
            return RCAResult()

        try:
            x = features.to(self.device)
            logits = self.gat(x)

            if logits.dim() == 1:
                probs = torch.softmax(logits, dim=-1)
            else:
                probs = torch.softmax(logits, dim=-1).squeeze(0)

            pred_idx = probs.argmax().item()

            # Map to service names
            # If GAT outputs per-service scores
            service_scores: Dict[str, float] = {}
            if len(probs) == NUM_SERVICES:
                for i in range(NUM_SERVICES):
                    service_scores[IDX_TO_SERVICE.get(i, f"svc_{i}")] = float(probs[i])
                root_service = IDX_TO_SERVICE.get(pred_idx, "unknown")
            else:
                root_service = IDX_TO_FAULT.get(pred_idx, "unknown")

            return RCAResult(
                root_cause_service=root_service,
                service_scores=service_scores,
                confidence=float(probs[pred_idx]) if len(probs) > 0 else 0.0,
            )
        except Exception as e:
            logger.debug("GAT RCA failed: %s", e)
            return RCAResult()

    # ── Stage 4: Ensemble Combination ───────────────────────────────

    def _determine_confidence(
        self,
        anomaly: AnomalyResult,
        classification: ClassificationResult,
        rca: RCAResult,
    ) -> Tuple[ConfidenceLevel, float]:
        """Compute overall confidence from individual model outputs.

        Args:
            anomaly: Anomaly detection result.
            classification: Classification result.
            rca: Root cause analysis result.

        Returns:
            (confidence_level, confidence_score) tuple.
        """
        scores: List[float] = []
        weights: List[float] = []

        if anomaly.combined_score > 0:
            # Higher anomaly score → higher confidence in anomaly detection
            scores.append(anomaly.combined_score if anomaly.is_anomaly else 1.0 - anomaly.combined_score)
            weights.append(self._weights.get("lstm_ae", 0.0) + self._weights.get("vae", 0.0))

        if classification.confidence > 0:
            scores.append(classification.confidence)
            weights.append(self._weights.get("transformer", 0.0))

        if rca.confidence > 0:
            scores.append(rca.confidence)
            weights.append(self._weights.get("gat", 0.0))

        # Weighted average
        total_w = sum(weights)
        if total_w > 0 and scores:
            conf_score = sum(s * w for s, w in zip(scores, weights)) / total_w
        else:
            conf_score = 0.0

        # Map to confidence level
        if conf_score >= self.cfg.high_confidence_threshold:
            level = ConfidenceLevel.HIGH
        elif conf_score >= self.cfg.medium_confidence_threshold:
            level = ConfidenceLevel.MEDIUM
        elif conf_score >= self.cfg.low_confidence_threshold:
            level = ConfidenceLevel.LOW
        else:
            level = ConfidenceLevel.UNKNOWN

        return level, conf_score

    def _generate_explanation(
        self,
        anomaly: AnomalyResult,
        classification: ClassificationResult,
        rca: RCAResult,
    ) -> str:
        """Generate human-readable explanation of the ensemble decision.

        Args:
            anomaly: Anomaly detection result.
            classification: Classification result.
            rca: Root cause analysis result.

        Returns:
            Explanation string.
        """
        parts: List[str] = []

        if anomaly.is_anomaly:
            parts.append(
                f"Anomaly detected (score={anomaly.combined_score:.2f}, "
                f"LSTM-AE={anomaly.lstm_ae_score:.2f}, VAE={anomaly.vae_score:.2f})"
            )
        else:
            parts.append("No anomaly detected in current window.")

        if classification.fault_type != "unknown_fault":
            parts.append(
                f"Fault classified as '{classification.fault_type}' "
                f"(confidence={classification.confidence:.2f}, severity={classification.severity})"
            )

        if rca.root_cause_service != "unknown":
            parts.append(
                f"Root cause attributed to '{rca.root_cause_service}' "
                f"(confidence={rca.confidence:.2f})"
            )

        return " | ".join(parts) if parts else "Insufficient data for analysis."

    # ── Public API ──────────────────────────────────────────────────

    def predict(
        self,
        features: torch.Tensor,
        graph_features: Optional[torch.Tensor] = None,
        service_idx: Optional[int] = None,
        use_distilled: bool = False,
    ) -> EnsemblePrediction:
        """Run the full ensemble pipeline on a single input.

        Args:
            features: Log window features, shape ``(1, seq_len, feat_dim)``.
            graph_features: Service-graph node features for GAT (optional).
            service_idx: Service index for conditional models.
            use_distilled: If True, use distilled GRU instead of Transformer.

        Returns:
            Complete EnsemblePrediction with all sub-results.
        """
        t0 = time.perf_counter()

        # Ensure batch dimension
        if features.dim() == 2:
            features = features.unsqueeze(0)

        # Stage 1: Anomaly detection
        anomaly = self._detect_anomaly(features, service_idx)

        # Stage 2: Classification (always run for severity)
        classification = self._classify(features, use_distilled)

        # Stage 3: RCA — only if anomaly detected (or if config says always)
        rca = RCAResult()
        if anomaly.is_anomaly or not self.cfg.skip_gat_if_no_anomaly:
            gat_input = graph_features if graph_features is not None else features
            rca = self._root_cause_analysis(gat_input)

        # Stage 4: Combine
        confidence_level, confidence_score = self._determine_confidence(
            anomaly, classification, rca
        )

        explanation = self._generate_explanation(anomaly, classification, rca)

        # Model contributions
        contributions: Dict[str, float] = {}
        if self.lstm_ae is not None:
            contributions["lstm_ae"] = anomaly.lstm_ae_score
        if self.vae is not None:
            contributions["vae"] = anomaly.vae_score
        if self.transformer is not None or self.distilled is not None:
            contributions["classifier"] = classification.confidence
        if self.gat is not None and anomaly.is_anomaly:
            contributions["gat"] = rca.confidence

        latency_ms = (time.perf_counter() - t0) * 1000.0

        return EnsemblePrediction(
            is_anomaly=anomaly.is_anomaly,
            fault_type=classification.fault_type if anomaly.is_anomaly else "normal",
            severity=classification.severity if anomaly.is_anomaly else "INFO",
            root_cause_service=rca.root_cause_service if anomaly.is_anomaly else "none",
            confidence=confidence_level,
            confidence_score=confidence_score,
            anomaly=anomaly,
            classification=classification,
            rca=rca,
            explanation=explanation,
            model_contributions=contributions,
            latency_ms=latency_ms,
        )

    def predict_batch(
        self,
        features_batch: torch.Tensor,
        graph_features: Optional[torch.Tensor] = None,
        use_distilled: bool = False,
    ) -> List[EnsemblePrediction]:
        """Run ensemble on a batch of inputs.

        Args:
            features_batch: Batch tensor, shape ``(B, seq_len, feat_dim)``.
            graph_features: Optional graph features.
            use_distilled: If True, use distilled model.

        Returns:
            List of EnsemblePrediction, one per sample.
        """
        results: List[EnsemblePrediction] = []
        for i in range(features_batch.size(0)):
            pred = self.predict(
                features_batch[i].unsqueeze(0),
                graph_features=graph_features,
                use_distilled=use_distilled,
            )
            results.append(pred)
        return results

    @property
    def available_models(self) -> List[str]:
        """List of currently loaded model names."""
        available: List[str] = []
        if self.transformer is not None:
            available.append("transformer")
        if self.lstm_ae is not None:
            available.append("lstm_ae")
        if self.gat is not None:
            available.append("gat")
        if self.vae is not None:
            available.append("vae")
        if self.distilled is not None:
            available.append("distilled")
        return available

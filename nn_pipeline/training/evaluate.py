"""
Evaluation module for all Neural Network Log Analyzer models.

Computes:
    - Classification metrics: accuracy, F1, precision, recall (macro/weighted)
    - ROC-AUC (one-vs-rest, for multi-class)
    - Confusion matrix
    - Anomaly detection metrics (for LSTM-AE / VAE)
    - Inference latency benchmarks (per-batch and per-sample)
    - JSON report export
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from nn_pipeline.config import (
    FAULT_TYPES,
    IDX_TO_FAULT,
    IDX_TO_LEVEL,
    LOG_LEVELS,
    MODEL_CHECKPOINTS_DIR,
    training_config,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Metric Computation Helpers
# ─────────────────────────────────────────────────────────────────────


def _safe_divide(num: float, den: float) -> float:
    """Safe division returning 0.0 when denominator is zero."""
    return num / den if den > 0 else 0.0


def compute_classification_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    y_prob: Optional[np.ndarray] = None,
    class_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compute classification metrics from numpy arrays.

    Args:
        y_true: Ground-truth labels, shape ``(N,)``.
        y_pred: Predicted labels, shape ``(N,)``.
        y_prob: Predicted probabilities, shape ``(N, C)`` (optional, for ROC-AUC).
        class_names: List of class names for labeling.

    Returns:
        Dict of metric name → value.
    """
    try:
        from sklearn.metrics import (
            accuracy_score,
            classification_report,
            confusion_matrix,
            f1_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )
    except ImportError:
        logger.warning(
            "scikit-learn not installed; computing basic metrics only."
        )
        return _basic_metrics(y_true, y_pred)

    n_classes = len(set(y_true.tolist()) | set(y_pred.tolist()))
    avg = "binary" if n_classes == 2 else "macro"

    results: Dict[str, Any] = {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "f1_macro": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
        "f1_weighted": float(
            f1_score(y_true, y_pred, average="weighted", zero_division=0)
        ),
        "precision_macro": float(
            precision_score(y_true, y_pred, average="macro", zero_division=0)
        ),
        "recall_macro": float(
            recall_score(y_true, y_pred, average="macro", zero_division=0)
        ),
        "precision_weighted": float(
            precision_score(y_true, y_pred, average="weighted", zero_division=0)
        ),
        "recall_weighted": float(
            recall_score(y_true, y_pred, average="weighted", zero_division=0)
        ),
    }

    # Confusion matrix
    cm = confusion_matrix(y_true, y_pred).tolist()
    results["confusion_matrix"] = cm

    # Per-class report
    labels = class_names or [str(i) for i in range(n_classes)]
    try:
        report = classification_report(
            y_true, y_pred, target_names=labels, output_dict=True, zero_division=0
        )
        results["per_class"] = {
            k: v
            for k, v in report.items()
            if k not in ("accuracy", "macro avg", "weighted avg")
        }
    except Exception:
        pass

    # ROC-AUC
    if y_prob is not None and n_classes > 1:
        try:
            if n_classes == 2:
                results["roc_auc"] = float(
                    roc_auc_score(y_true, y_prob[:, 1])
                )
            else:
                results["roc_auc_ovr"] = float(
                    roc_auc_score(
                        y_true,
                        y_prob,
                        multi_class="ovr",
                        average="macro",
                    )
                )
        except Exception as e:
            logger.debug("ROC-AUC computation failed: %s", e)

    results["num_samples"] = int(len(y_true))
    results["num_classes"] = n_classes

    return results


def _basic_metrics(
    y_true: np.ndarray, y_pred: np.ndarray
) -> Dict[str, Any]:
    """Minimal metric computation without sklearn."""
    correct = int((y_true == y_pred).sum())
    total = len(y_true)
    return {
        "accuracy": _safe_divide(correct, total),
        "num_samples": total,
        "num_correct": correct,
    }


def compute_anomaly_metrics(
    is_anomaly_true: np.ndarray,
    anomaly_scores: np.ndarray,
    threshold: float,
) -> Dict[str, Any]:
    """Compute anomaly detection metrics.

    Args:
        is_anomaly_true: Binary ground-truth (1 = anomaly), shape ``(N,)``.
        anomaly_scores: Continuous anomaly scores, shape ``(N,)``.
        threshold: Score threshold above which we declare anomaly.

    Returns:
        Dict of metric name → value.
    """
    is_anomaly_pred = (anomaly_scores >= threshold).astype(int)

    tp = int(((is_anomaly_pred == 1) & (is_anomaly_true == 1)).sum())
    fp = int(((is_anomaly_pred == 1) & (is_anomaly_true == 0)).sum())
    fn = int(((is_anomaly_pred == 0) & (is_anomaly_true == 1)).sum())
    tn = int(((is_anomaly_pred == 0) & (is_anomaly_true == 0)).sum())

    precision = _safe_divide(tp, tp + fp)
    recall = _safe_divide(tp, tp + fn)
    f1 = _safe_divide(2 * precision * recall, precision + recall)

    results: Dict[str, Any] = {
        "threshold": threshold,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "total_samples": int(len(is_anomaly_true)),
        "total_anomalies": int(is_anomaly_true.sum()),
    }

    # AUC if sklearn available
    try:
        from sklearn.metrics import roc_auc_score

        results["roc_auc"] = float(
            roc_auc_score(is_anomaly_true, anomaly_scores)
        )
    except Exception:
        pass

    return results


# ─────────────────────────────────────────────────────────────────────
# Latency Benchmark
# ─────────────────────────────────────────────────────────────────────


def benchmark_latency(
    model: nn.Module,
    sample_input: torch.Tensor,
    device: torch.device,
    num_warmup: int = 10,
    num_runs: int = 100,
) -> Dict[str, float]:
    """Benchmark inference latency for a model.

    Args:
        model: Model in eval mode.
        sample_input: A single input tensor (or batch of size 1).
        device: Device to run inference on.
        num_warmup: Number of warmup forward passes.
        num_runs: Number of timed forward passes.

    Returns:
        Dict with latency stats in milliseconds.
    """
    model.eval()
    sample_input = sample_input.to(device)

    # Warmup
    with torch.no_grad():
        for _ in range(num_warmup):
            _ = model(sample_input)

    # Synchronize for accurate GPU timing
    if device.type == "cuda":
        torch.cuda.synchronize()

    latencies: List[float] = []
    with torch.no_grad():
        for _ in range(num_runs):
            if device.type == "cuda":
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            _ = model(sample_input)
            if device.type == "cuda":
                torch.cuda.synchronize()
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000.0)  # ms

    arr = np.array(latencies)
    return {
        "mean_ms": float(arr.mean()),
        "std_ms": float(arr.std()),
        "median_ms": float(np.median(arr)),
        "p95_ms": float(np.percentile(arr, 95)),
        "p99_ms": float(np.percentile(arr, 99)),
        "min_ms": float(arr.min()),
        "max_ms": float(arr.max()),
        "num_runs": num_runs,
    }


# ─────────────────────────────────────────────────────────────────────
# Model Evaluator Class
# ─────────────────────────────────────────────────────────────────────


class ModelEvaluator:
    """Runs evaluation across a DataLoader and produces a full report.

    Example::

        evaluator = ModelEvaluator(model, device, "transformer_classifier")
        report = evaluator.evaluate(test_loader)
        evaluator.save_report(report, Path("reports/transformer.json"))
    """

    def __init__(
        self,
        model: nn.Module,
        device: torch.device,
        model_name: str,
        class_names: Optional[List[str]] = None,
        forward_fn: Optional[
            Callable[[nn.Module, Any, torch.device], Tuple[torch.Tensor, torch.Tensor]]
        ] = None,
    ) -> None:
        """
        Args:
            model: Trained model.
            device: Device for inference.
            model_name: Name for reports.
            class_names: Optional list of class names.
            forward_fn: Custom forward function ``(model, batch, device) → (logits, targets)``.
        """
        self.model = model.to(device)
        self.device = device
        self.model_name = model_name
        self.class_names = class_names
        self._forward_fn = forward_fn

    @staticmethod
    def _default_forward(
        model: nn.Module,
        batch: Any,
        device: torch.device,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Default forward: batch = (inputs, targets)."""
        inputs, targets = batch
        inputs = inputs.to(device)
        targets = targets.to(device)
        logits = model(inputs)
        return logits, targets

    @torch.no_grad()
    def evaluate(
        self,
        loader: DataLoader,
        compute_roc: bool = True,
    ) -> Dict[str, Any]:
        """Run full evaluation on a DataLoader.

        Args:
            loader: Test/validation DataLoader.
            compute_roc: Whether to compute ROC-AUC from probabilities.

        Returns:
            Full evaluation report as a dict.
        """
        self.model.eval()
        forward_fn = self._forward_fn or self._default_forward

        all_preds: List[np.ndarray] = []
        all_targets: List[np.ndarray] = []
        all_probs: List[np.ndarray] = []
        total_loss = 0.0
        num_batches = 0

        for batch in loader:
            logits, targets = forward_fn(self.model, batch, self.device)

            probs = torch.softmax(logits, dim=-1)
            preds = logits.argmax(dim=-1)

            all_preds.append(preds.cpu().numpy())
            all_targets.append(targets.cpu().numpy())
            if compute_roc:
                all_probs.append(probs.cpu().numpy())
            num_batches += 1

        y_pred = np.concatenate(all_preds)
        y_true = np.concatenate(all_targets)
        y_prob = np.concatenate(all_probs) if compute_roc and all_probs else None

        report = compute_classification_metrics(
            y_true, y_pred, y_prob, self.class_names
        )
        report["model_name"] = self.model_name
        return report

    @torch.no_grad()
    def evaluate_anomaly(
        self,
        loader: DataLoader,
        score_fn: Callable[
            [nn.Module, Any, torch.device], Tuple[np.ndarray, np.ndarray]
        ],
        threshold: float,
    ) -> Dict[str, Any]:
        """Evaluate anomaly detection models (LSTM-AE, VAE).

        Args:
            loader: Test DataLoader.
            score_fn: ``(model, batch, device) → (anomaly_scores, is_anomaly_true)``
                both as numpy arrays.
            threshold: Anomaly score threshold.

        Returns:
            Anomaly detection evaluation report.
        """
        self.model.eval()

        all_scores: List[np.ndarray] = []
        all_labels: List[np.ndarray] = []

        for batch in loader:
            scores, labels = score_fn(self.model, batch, self.device)
            all_scores.append(scores)
            all_labels.append(labels)

        scores_np = np.concatenate(all_scores)
        labels_np = np.concatenate(all_labels)

        report = compute_anomaly_metrics(labels_np, scores_np, threshold)
        report["model_name"] = self.model_name
        return report

    def benchmark(
        self,
        sample_input: torch.Tensor,
        num_runs: int = 100,
    ) -> Dict[str, float]:
        """Benchmark inference latency.

        Args:
            sample_input: Example input tensor.
            num_runs: Number of timed iterations.

        Returns:
            Latency statistics.
        """
        return benchmark_latency(
            self.model,
            sample_input,
            self.device,
            num_runs=num_runs,
        )

    @staticmethod
    def save_report(
        report: Dict[str, Any],
        path: Optional[Path] = None,
        model_name: Optional[str] = None,
    ) -> Path:
        """Save evaluation report as JSON.

        Args:
            report: Evaluation results dict.
            path: Output file path. Defaults to checkpoints dir.
            model_name: Used to build default path if ``path`` is None.

        Returns:
            Path to saved JSON file.
        """
        if path is None:
            name = model_name or report.get("model_name", "unknown")
            path = MODEL_CHECKPOINTS_DIR / name / "evaluation_report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info("Saved evaluation report to %s", path)
        return path


# ─────────────────────────────────────────────────────────────────────
# Convenience: Evaluate all models
# ─────────────────────────────────────────────────────────────────────


def evaluate_all_models(
    models: Dict[str, Tuple[nn.Module, DataLoader]],
    device: torch.device,
    output_dir: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Evaluate multiple models and aggregate reports.

    Args:
        models: Dict of ``model_name → (model, test_loader)``.
        device: Target device.
        output_dir: Directory for per-model reports.

    Returns:
        Dict of ``model_name → evaluation_report``.
    """
    all_reports: Dict[str, Dict[str, Any]] = {}

    for name, (model, loader) in models.items():
        logger.info("Evaluating %s ...", name)

        # Determine class names based on model type
        class_names: Optional[List[str]] = None
        if "fault" in name.lower():
            class_names = FAULT_TYPES
        elif "severity" in name.lower() or "level" in name.lower():
            class_names = LOG_LEVELS

        evaluator = ModelEvaluator(model, device, name, class_names)
        report = evaluator.evaluate(loader)

        out = output_dir or MODEL_CHECKPOINTS_DIR
        ModelEvaluator.save_report(report, out / name / "evaluation_report.json")
        all_reports[name] = report

    # Summary report
    summary: Dict[str, Any] = {
        "models_evaluated": list(all_reports.keys()),
        "summary": {},
    }
    for name, rep in all_reports.items():
        summary["summary"][name] = {
            "accuracy": rep.get("accuracy"),
            "f1_macro": rep.get("f1_macro"),
            "num_samples": rep.get("num_samples"),
        }

    summary_path = (output_dir or MODEL_CHECKPOINTS_DIR) / "evaluation_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("All evaluations complete. Summary: %s", summary_path)

    return all_reports

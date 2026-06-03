"""
Heuristic labeler for log windows.

Assigns two kinds of labels to each ``LogWindow``:

1. **Severity label** — the dominant (highest) log level in the window.
   Maps to ``config.LOG_LEVELS`` indices: INFO=0, WARNING=1, ERROR=2.

2. **Fault-type label** — the inferred fault category based on keyword
   patterns, error rates, and service-dependency analysis.  Maps to
   ``config.FAULT_TYPES`` indices: normal=0, service_failure=1,
   dependency_failure=2, resource_failure=3, network_failure=4,
   unknown_fault=5.

These heuristic labels serve as training targets for the supervised
models (Transformer classifier, knowledge-distilled GRU) and as
weak supervision for the anomaly detectors (LSTM-AE, VAE).

Usage::

    labeler = WindowLabeler()
    result = labeler.label(window)
    print(result.severity_label, result.fault_label)

    # Batch
    results = labeler.label_batch(windows)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from nn_pipeline.config import (
    FAULT_TO_IDX,
    FAULT_TYPES,
    IDX_TO_FAULT,
    LEVEL_TO_IDX,
    LOG_LEVELS,
    NUM_FAULT_TYPES,
    NUM_LEVELS,
    SERVICE_DEPENDENCIES,
    SERVICES,
    SERVICE_TO_IDX,
)
from nn_pipeline.data.log_parser import LogEntry
from nn_pipeline.data.window_builder import LogWindow

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Fault-detection keyword patterns
# ─────────────────────────────────────────────────────────────────────

# Network-related patterns
_NETWORK_PATTERNS: List[re.Pattern] = [
    re.compile(r"connection\s+refused", re.I),
    re.compile(r"connection\s+error", re.I),
    re.compile(r"i/o\s+timeout", re.I),
    re.compile(r"dial\s+tcp", re.I),
    re.compile(r"transport:\s+Error", re.I),
    re.compile(r"Unavailable\s+desc", re.I),
    re.compile(r"dns\s+lookup\s+failed", re.I),
    re.compile(r"connect:\s+connection\s+refused", re.I),
    re.compile(r"no\s+route\s+to\s+host", re.I),
]

# Resource-exhaustion patterns
_RESOURCE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\boom\b", re.I),
    re.compile(r"out\s+of\s+memory", re.I),
    re.compile(r"memory\s+limit", re.I),
    re.compile(r"cpu\s+exhaustion", re.I),
    re.compile(r"resource\s+exhausted", re.I),
    re.compile(r"disk\s+full", re.I),
    re.compile(r"cannot\s+allocate", re.I),
    re.compile(r"too\s+many\s+open\s+files", re.I),
]

# Service-crash patterns
_SERVICE_FAILURE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bcrash", re.I),
    re.compile(r"\bpanic\b", re.I),
    re.compile(r"fatal\s+error", re.I),
    re.compile(r"segmentation\s+fault", re.I),
    re.compile(r"unhandled\s+exception", re.I),
    re.compile(r"failed\s+to\s+start", re.I),
    re.compile(r"process\s+exited", re.I),
]

# Dependency-failure patterns (errors referencing other services)
_DEPENDENCY_KEYWORDS: List[str] = [
    "rpc error",
    "upstream",
    "downstream",
    "failed to get",
    "failed to complete",
    "could not retrieve",
    "failed to charge",
    "failed to retrieve",
]


# ─────────────────────────────────────────────────────────────────────
# LabelResult dataclass
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LabelResult:
    """Labels assigned to a single window.

    Attributes:
        severity_label: Index into ``LOG_LEVELS`` (0=INFO, 1=WARNING, 2=ERROR).
        severity_name: Human-readable severity string.
        fault_label: Index into ``FAULT_TYPES`` (0=normal .. 5=unknown_fault).
        fault_name: Human-readable fault type string.
        confidence: Rough confidence score for the fault label (0.0–1.0).
        evidence: List of short strings describing what triggered the label.
        is_anomalous: True if any fault (non-normal) was detected.
        affected_services: Set of services mentioned in error messages.
    """
    severity_label: int
    severity_name: str
    fault_label: int
    fault_name: str
    confidence: float
    evidence: List[str]
    is_anomalous: bool
    affected_services: Set[str]


# ─────────────────────────────────────────────────────────────────────
# WindowLabeler
# ─────────────────────────────────────────────────────────────────────


class WindowLabeler:
    """Assigns heuristic severity and fault-type labels to log windows.

    Configurable thresholds control how aggressively faults are detected.

    Args:
        error_rate_threshold: Fraction of ERROR entries (0-1) above which
                              a window is considered anomalous.
        warning_rate_threshold: Fraction of WARNING entries (0-1) that
                                contribute to anomaly scoring.
        min_entries_for_label: Minimum number of real (non-padding) entries
                               required in a window to assign a fault label.
    """

    def __init__(
        self,
        *,
        error_rate_threshold: float = 0.1,
        warning_rate_threshold: float = 0.3,
        min_entries_for_label: int = 5,
    ) -> None:
        self.error_rate_threshold = error_rate_threshold
        self.warning_rate_threshold = warning_rate_threshold
        self.min_entries_for_label = min_entries_for_label

        # Build reverse dependency lookup: service → set of services that depend on it
        self._dependents: Dict[str, Set[str]] = {s: set() for s in SERVICES}
        for parent, children in SERVICE_DEPENDENCIES.items():
            for child in children:
                if child in self._dependents:
                    self._dependents[child].add(parent)

    # ─────────────────────────────────────────────────────────────────
    # Severity labeling
    # ─────────────────────────────────────────────────────────────────

    def _compute_severity(self, window: LogWindow) -> Tuple[int, str]:
        """Determine the dominant severity level for a window.

        Returns the *highest* severity seen (ERROR > WARNING > INFO).
        """
        max_level_idx = 0
        for entry in window.entries:
            if entry.level_idx > max_level_idx:
                max_level_idx = entry.level_idx
        severity_name = LOG_LEVELS[min(max_level_idx, NUM_LEVELS - 1)]
        return max_level_idx, severity_name

    # ─────────────────────────────────────────────────────────────────
    # Fault-type labeling
    # ─────────────────────────────────────────────────────────────────

    def _detect_fault(
        self, window: LogWindow
    ) -> Tuple[int, str, float, List[str], Set[str]]:
        """Heuristic fault-type detection for a window.

        Scans all ERROR and WARNING messages for keyword patterns and
        cross-references with the service dependency graph.

        Returns:
            (fault_label, fault_name, confidence, evidence_list, affected_services)
        """
        evidence: List[str] = []
        affected_services: Set[str] = set()

        # Scores for each fault type (higher = more likely)
        scores: Dict[str, float] = {ft: 0.0 for ft in FAULT_TYPES}
        scores["normal"] = 0.5  # Prior bias toward normal

        n_entries = len(window.entries)
        if n_entries < self.min_entries_for_label:
            return (
                FAULT_TO_IDX["normal"],
                "normal",
                0.9,
                ["Insufficient entries for labeling"],
                affected_services,
            )

        error_rate = window.error_count / n_entries
        warning_rate = window.warning_count / n_entries

        # ── Check error rate ──
        if error_rate >= self.error_rate_threshold:
            scores["normal"] -= 0.5
            evidence.append(f"High error rate: {error_rate:.1%}")

        # Collect error/warning messages for pattern matching
        error_messages: List[Tuple[LogEntry, str]] = []
        for entry in window.entries:
            if entry.level in ("ERROR", "WARNING"):
                msg = entry.message.lower()
                error_messages.append((entry, msg))
                affected_services.add(entry.service)

        if not error_messages:
            return (
                FAULT_TO_IDX["normal"],
                "normal",
                0.9,
                ["No errors or warnings"],
                affected_services,
            )

        # ── Network failure detection ──
        for entry, msg in error_messages:
            for pattern in _NETWORK_PATTERNS:
                if pattern.search(msg):
                    scores["network_failure"] += 1.0
                    evidence.append(
                        f"Network pattern in {entry.service}: {pattern.pattern}"
                    )
                    break

        # ── Resource failure detection ──
        for entry, msg in error_messages:
            for pattern in _RESOURCE_PATTERNS:
                if pattern.search(msg):
                    scores["resource_failure"] += 1.0
                    evidence.append(
                        f"Resource pattern in {entry.service}: {pattern.pattern}"
                    )
                    break

        # ── Service failure detection ──
        for entry, msg in error_messages:
            for pattern in _SERVICE_FAILURE_PATTERNS:
                if pattern.search(msg):
                    scores["service_failure"] += 1.0
                    evidence.append(
                        f"Service failure pattern in {entry.service}: {pattern.pattern}"
                    )
                    break

        # ── Dependency failure detection ──
        for entry, msg in error_messages:
            for kw in _DEPENDENCY_KEYWORDS:
                if kw in msg:
                    scores["dependency_failure"] += 0.8
                    evidence.append(
                        f"Dependency keyword '{kw}' in {entry.service}"
                    )
                    # Check if the error references a known dependency
                    for svc in SERVICES:
                        if svc in msg and svc != entry.service:
                            scores["dependency_failure"] += 0.5
                            evidence.append(
                                f"Cross-service reference: {entry.service} → {svc}"
                            )
                            affected_services.add(svc)
                    break

        # ── Dependency-graph analysis ──
        # If multiple services in the same dependency chain show errors,
        # boost dependency_failure
        error_services = {entry.service for entry, _ in error_messages}
        for svc in error_services:
            deps = SERVICE_DEPENDENCIES.get(svc, [])
            for dep in deps:
                if dep in error_services:
                    scores["dependency_failure"] += 0.5
                    evidence.append(
                        f"Dependency chain: {svc} depends on {dep}, both have errors"
                    )

        # ── Select the highest-scoring fault type ──
        best_fault = max(scores, key=scores.get)  # type: ignore[arg-type]
        best_score = scores[best_fault]
        total_score = sum(scores.values())

        # Confidence is the relative score of the best fault type
        confidence = best_score / total_score if total_score > 0 else 0.5

        # If no fault-specific pattern was detected but there are errors,
        # label as unknown_fault
        non_normal_scores = {
            k: v for k, v in scores.items() if k != "normal"
        }
        if best_fault == "normal" and error_rate >= self.error_rate_threshold:
            best_fault = "unknown_fault"
            confidence = 0.3
            evidence.append("Errors present but no specific pattern matched")

        fault_label = FAULT_TO_IDX[best_fault]
        return fault_label, best_fault, confidence, evidence, affected_services

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    def label(self, window: LogWindow) -> LabelResult:
        """Assign labels to a single window.

        Args:
            window: A ``LogWindow`` from the window builder.

        Returns:
            ``LabelResult`` with severity, fault type, and metadata.
        """
        severity_label, severity_name = self._compute_severity(window)
        fault_label, fault_name, confidence, evidence, affected = self._detect_fault(
            window
        )

        return LabelResult(
            severity_label=severity_label,
            severity_name=severity_name,
            fault_label=fault_label,
            fault_name=fault_name,
            confidence=confidence,
            evidence=evidence,
            is_anomalous=(fault_name != "normal"),
            affected_services=affected,
        )

    def label_batch(self, windows: List[LogWindow]) -> List[LabelResult]:
        """Label a list of windows.

        Args:
            windows: List of ``LogWindow`` objects.

        Returns:
            List of ``LabelResult``, one per window.
        """
        results = [self.label(w) for w in windows]

        # Log summary statistics
        fault_counts: Dict[str, int] = {ft: 0 for ft in FAULT_TYPES}
        for r in results:
            fault_counts[r.fault_name] += 1

        logger.info(
            "Labeled %d windows — distribution: %s",
            len(results),
            ", ".join(f"{k}={v}" for k, v in fault_counts.items() if v > 0),
        )
        return results

    def compute_label_statistics(
        self, results: List[LabelResult]
    ) -> Dict[str, Dict[str, int]]:
        """Compute label distribution statistics.

        Returns:
            Dict with "severity" and "fault" keys, each mapping to
            {label_name: count}.
        """
        severity_dist: Dict[str, int] = {lvl: 0 for lvl in LOG_LEVELS}
        fault_dist: Dict[str, int] = {ft: 0 for ft in FAULT_TYPES}

        for r in results:
            severity_dist[r.severity_name] += 1
            fault_dist[r.fault_name] += 1

        return {
            "severity": severity_dist,
            "fault": fault_dist,
        }

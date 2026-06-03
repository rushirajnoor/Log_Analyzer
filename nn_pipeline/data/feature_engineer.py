"""
Feature engineering for individual log entries.

Converts each ``LogEntry`` + its ``TemplateResult`` into a fixed-size numeric
feature vector of dimension ``DataConfig.per_entry_feature_dim`` (default 20).

Feature layout (20 dims total):
  [ 0]        log-level one-hot: INFO     (0 or 1)
  [ 1]        log-level one-hot: WARNING  (0 or 1)
  [ 2]        log-level one-hot: ERROR    (0 or 1)
  [ 3]        template_id / max_templates (normalized 0-1)
  [ 4]        message_length / max_message_length (normalized 0-1)
  [ 5- 19]    error-keyword indicator flags (15 dims, one per keyword)

The 15 error keywords are defined in ``config.ERROR_KEYWORDS``.

Usage::

    from nn_pipeline.data.log_parser import LogEntry
    from nn_pipeline.data.template_extractor import TemplateResult

    engineer = FeatureEngineer()
    vector = engineer.transform_entry(log_entry, template_result)
    # vector.shape == (20,)

    # Batch mode
    matrix = engineer.transform_batch(entries, template_results)
    # matrix.shape == (N, 20)
"""

from __future__ import annotations

import logging
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from nn_pipeline.config import (
    ERROR_KEYWORDS,
    LOG_LEVELS,
    LEVEL_TO_IDX,
    NUM_LEVELS,
    NUM_SERVICES,
    SERVICE_TO_IDX,
    data_config,
)
from nn_pipeline.data.log_parser import LogEntry
from nn_pipeline.data.template_extractor import TemplateResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Compile keyword patterns once
# ─────────────────────────────────────────────────────────────────────

_KEYWORD_PATTERNS: List[re.Pattern] = [
    re.compile(re.escape(kw), re.IGNORECASE) for kw in ERROR_KEYWORDS
]

# Feature index boundaries
_LEVEL_START = 0
_LEVEL_END = NUM_LEVELS                        # 3
_TEMPLATE_IDX = _LEVEL_END                     # 3
_MSG_LEN_IDX = _TEMPLATE_IDX + 1              # 4
_KEYWORD_START = _MSG_LEN_IDX + 1             # 5
_KEYWORD_END = _KEYWORD_START + len(ERROR_KEYWORDS)  # 20
FEATURE_DIM = _KEYWORD_END                     # 20

# Sanity check against config
assert FEATURE_DIM == data_config.per_entry_feature_dim, (
    f"Feature dim mismatch: computed {FEATURE_DIM} vs config {data_config.per_entry_feature_dim}"
)


class FeatureEngineer:
    """Transforms log entries into fixed-size numerical feature vectors.

    Thread-safe: no mutable state after construction.

    Attributes:
        feature_dim: The output dimension of the feature vector (default 20).
    """

    def __init__(self) -> None:
        self.feature_dim: int = FEATURE_DIM
        self._max_templates: int = data_config.max_templates
        self._max_msg_len: int = data_config.max_message_length

    def transform_entry(
        self,
        entry: LogEntry,
        template_result: Optional[TemplateResult] = None,
    ) -> List[float]:
        """Convert a single LogEntry into a feature vector.

        Args:
            entry: The parsed log entry.
            template_result: Optional template extraction result. If None,
                             the template_id feature is set to 0.

        Returns:
            List of floats with length ``self.feature_dim``.
        """
        features = [0.0] * self.feature_dim

        # ── Log-level one-hot (dims 0-2) ──
        level_idx = entry.level_idx
        if 0 <= level_idx < NUM_LEVELS:
            features[_LEVEL_START + level_idx] = 1.0

        # ── Template ID normalized (dim 3) ──
        if template_result is not None:
            features[_TEMPLATE_IDX] = (
                template_result.template_id / max(self._max_templates, 1)
            )

        # ── Message length normalized (dim 4) ──
        features[_MSG_LEN_IDX] = min(
            len(entry.message) / max(self._max_msg_len, 1), 1.0
        )

        # ── Error keyword indicators (dims 5-19) ──
        msg_lower = entry.message.lower()
        for i, pattern in enumerate(_KEYWORD_PATTERNS):
            if pattern.search(msg_lower):
                features[_KEYWORD_START + i] = 1.0

        return features

    def transform_batch(
        self,
        entries: Sequence[LogEntry],
        template_results: Optional[Sequence[Optional[TemplateResult]]] = None,
    ) -> List[List[float]]:
        """Convert a batch of log entries into a feature matrix.

        Args:
            entries: Sequence of parsed log entries.
            template_results: Optional sequence of template results (same length).
                              If None, all template_id features default to 0.

        Returns:
            List of feature vectors, shape ``(len(entries), feature_dim)``.
        """
        if template_results is None:
            template_results = [None] * len(entries)

        if len(entries) != len(template_results):
            raise ValueError(
                f"entries and template_results length mismatch: "
                f"{len(entries)} vs {len(template_results)}"
            )

        return [
            self.transform_entry(entry, tr)
            for entry, tr in zip(entries, template_results)
        ]

    def get_feature_names(self) -> List[str]:
        """Return human-readable names for each feature dimension.

        Returns:
            List of strings, length ``feature_dim``.
        """
        names: List[str] = []
        # Level one-hot
        for level in LOG_LEVELS:
            names.append(f"level_{level}")
        # Template
        names.append("template_id_norm")
        # Message length
        names.append("msg_length_norm")
        # Keywords
        for kw in ERROR_KEYWORDS:
            names.append(f"kw_{kw}")
        return names

    @staticmethod
    def compute_inter_entry_features(
        entry: LogEntry,
        prev_entry: Optional[LogEntry],
    ) -> Dict[str, float]:
        """Compute features that depend on the relationship between consecutive entries.

        These are *not* included in the per-entry vector but can be used
        as additional window-level or edge features.

        Args:
            entry: Current log entry.
            prev_entry: Previous log entry (or None for the first entry).

        Returns:
            Dict with feature name → value:
              - ``time_delta``: seconds since previous entry
              - ``same_service``: 1.0 if same service as previous, else 0.0
              - ``level_change``: 1.0 if level changed from previous, else 0.0
        """
        if prev_entry is None:
            return {
                "time_delta": 0.0,
                "same_service": 0.0,
                "level_change": 0.0,
            }
        return {
            "time_delta": max(entry.timestamp_unix - prev_entry.timestamp_unix, 0.0),
            "same_service": 1.0 if entry.service == prev_entry.service else 0.0,
            "level_change": 1.0 if entry.level != prev_entry.level else 0.0,
        }

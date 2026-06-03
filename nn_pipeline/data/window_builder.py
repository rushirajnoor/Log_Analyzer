"""
Sliding-window builder for the log sequence pipeline.

Slices a chronologically sorted list of ``LogEntry`` objects (with their
feature vectors) into fixed-size windows for consumption by sequence models
(Transformer, BiLSTM Autoencoder).

Two windowing strategies:
  1. **Count-based** — fixed number of log entries per window (``window_size``),
     advanced by ``window_stride`` entries.  Default: 64 entries, stride 32.
  2. **Time-based** — all entries within a ``time_window_seconds`` duration,
     advanced by ``time_window_stride_seconds``.  Default: 30 s, stride 15 s.

Each window is represented as a ``LogWindow`` dataclass containing the raw
entries, their pre-computed feature matrix, and metadata (service distribution,
time span, etc.).

Usage::

    from nn_pipeline.data.window_builder import WindowBuilder, WindowStrategy

    builder = WindowBuilder(strategy=WindowStrategy.COUNT)
    windows = builder.build(entries, feature_matrix)
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from nn_pipeline.config import (
    SERVICES,
    SERVICE_TO_IDX,
    NUM_SERVICES,
    data_config,
)
from nn_pipeline.data.log_parser import LogEntry

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Enums & dataclasses
# ─────────────────────────────────────────────────────────────────────


class WindowStrategy(enum.Enum):
    """Windowing strategy selector."""
    COUNT = "count"  # Fixed number of entries per window
    TIME = "time"    # Fixed time duration per window


@dataclass
class LogWindow:
    """A single window of log entries with pre-computed features.

    Attributes:
        window_id: Unique sequential identifier for this window.
        entries: The log entries in this window (ordered by timestamp).
        features: Feature matrix, shape ``(num_entries, feature_dim)``.
                  For count-based windows this is always ``(window_size, feature_dim)``.
                  For time-based windows the length may vary.
        start_time: Unix timestamp of the first entry in the window.
        end_time: Unix timestamp of the last entry in the window.
        service_counts: Dict mapping service name → count of entries in this window.
        dominant_service: The service with the most entries in this window.
        error_count: Number of ERROR-level entries in this window.
        warning_count: Number of WARNING-level entries in this window.
    """
    window_id: int
    entries: List[LogEntry]
    features: List[List[float]]
    start_time: float
    end_time: float
    service_counts: Dict[str, int] = field(default_factory=dict)
    dominant_service: str = ""
    error_count: int = 0
    warning_count: int = 0

    @property
    def duration(self) -> float:
        """Time span of this window in seconds."""
        return self.end_time - self.start_time

    @property
    def size(self) -> int:
        """Number of entries in this window."""
        return len(self.entries)


# ─────────────────────────────────────────────────────────────────────
# Padding helper
# ─────────────────────────────────────────────────────────────────────


def _pad_features(
    features: List[List[float]],
    target_len: int,
    feature_dim: int,
) -> List[List[float]]:
    """Pad or truncate a feature matrix to ``target_len`` rows.

    Padding uses zero vectors.  Truncation keeps the *last* ``target_len`` rows
    (most recent entries).

    Args:
        features: Feature matrix to pad/truncate.
        target_len: Desired number of rows.
        feature_dim: Width of each feature vector.

    Returns:
        Padded/truncated feature matrix.
    """
    current_len = len(features)
    if current_len >= target_len:
        # Truncate: keep most recent entries
        return features[-target_len:]
    # Pad with zero vectors at the front (older = less important)
    padding = [[0.0] * feature_dim] * (target_len - current_len)
    return padding + features


# ─────────────────────────────────────────────────────────────────────
# Window metadata computation
# ─────────────────────────────────────────────────────────────────────


def _compute_window_metadata(entries: List[LogEntry]) -> Tuple[Dict[str, int], str, int, int]:
    """Compute aggregate metadata for a window of entries.

    Returns:
        (service_counts, dominant_service, error_count, warning_count)
    """
    service_counts: Dict[str, int] = {}
    error_count = 0
    warning_count = 0

    for entry in entries:
        service_counts[entry.service] = service_counts.get(entry.service, 0) + 1
        if entry.level == "ERROR":
            error_count += 1
        elif entry.level == "WARNING":
            warning_count += 1

    dominant_service = max(service_counts, key=service_counts.get) if service_counts else ""

    return service_counts, dominant_service, error_count, warning_count


# ─────────────────────────────────────────────────────────────────────
# WindowBuilder
# ─────────────────────────────────────────────────────────────────────


class WindowBuilder:
    """Builds sliding windows over log entry sequences.

    Args:
        strategy: Windowing strategy (COUNT or TIME).
        window_size: Number of entries per window (COUNT strategy).
        window_stride: Stride between windows (COUNT strategy).
        time_window_seconds: Duration of each window (TIME strategy).
        time_window_stride_seconds: Stride between windows (TIME strategy).
        pad_incomplete: If True, pad the last incomplete window to
                        ``window_size`` with zero vectors.  If False,
                        drop incomplete windows.
        feature_dim: Width of the feature vectors (for padding).
    """

    def __init__(
        self,
        strategy: WindowStrategy = WindowStrategy.COUNT,
        *,
        window_size: Optional[int] = None,
        window_stride: Optional[int] = None,
        time_window_seconds: Optional[float] = None,
        time_window_stride_seconds: Optional[float] = None,
        pad_incomplete: bool = True,
        feature_dim: Optional[int] = None,
    ) -> None:
        self.strategy = strategy
        self.window_size = window_size or data_config.window_size
        self.window_stride = window_stride or data_config.window_stride
        self.time_window_seconds = time_window_seconds or data_config.time_window_seconds
        self.time_window_stride_seconds = (
            time_window_stride_seconds or data_config.time_window_stride_seconds
        )
        self.pad_incomplete = pad_incomplete
        self.feature_dim = feature_dim or data_config.per_entry_feature_dim

    def build(
        self,
        entries: List[LogEntry],
        features: List[List[float]],
    ) -> List[LogWindow]:
        """Build windows from entries and their corresponding feature vectors.

        Args:
            entries: Chronologically sorted list of log entries.
            features: Feature matrix aligned with ``entries`` (same length).

        Returns:
            List of ``LogWindow`` objects.

        Raises:
            ValueError: If ``entries`` and ``features`` lengths differ.
        """
        if len(entries) != len(features):
            raise ValueError(
                f"entries ({len(entries)}) and features ({len(features)}) "
                f"length mismatch"
            )

        if not entries:
            return []

        if self.strategy == WindowStrategy.COUNT:
            return self._build_count_windows(entries, features)
        elif self.strategy == WindowStrategy.TIME:
            return self._build_time_windows(entries, features)
        else:
            raise ValueError(f"Unknown strategy: {self.strategy}")

    def _build_count_windows(
        self,
        entries: List[LogEntry],
        features: List[List[float]],
    ) -> List[LogWindow]:
        """Build fixed-count sliding windows."""
        windows: List[LogWindow] = []
        n = len(entries)
        window_id = 0

        start = 0
        while start < n:
            end = min(start + self.window_size, n)
            window_entries = entries[start:end]
            window_features = features[start:end]

            # Skip or pad incomplete windows
            if len(window_entries) < self.window_size:
                if not self.pad_incomplete:
                    break
                window_features = _pad_features(
                    window_features, self.window_size, self.feature_dim
                )

            service_counts, dominant, errors, warnings = _compute_window_metadata(
                window_entries
            )

            window = LogWindow(
                window_id=window_id,
                entries=window_entries,
                features=window_features,
                start_time=window_entries[0].timestamp_unix,
                end_time=window_entries[-1].timestamp_unix,
                service_counts=service_counts,
                dominant_service=dominant,
                error_count=errors,
                warning_count=warnings,
            )
            windows.append(window)
            window_id += 1
            start += self.window_stride

        logger.info(
            "Built %d count-based windows (size=%d, stride=%d) from %d entries",
            len(windows),
            self.window_size,
            self.window_stride,
            n,
        )
        return windows

    def _build_time_windows(
        self,
        entries: List[LogEntry],
        features: List[List[float]],
    ) -> List[LogWindow]:
        """Build time-based sliding windows."""
        windows: List[LogWindow] = []
        n = len(entries)

        if n == 0:
            return windows

        global_start = entries[0].timestamp_unix
        global_end = entries[-1].timestamp_unix
        window_id = 0

        t_start = global_start
        while t_start <= global_end:
            t_end = t_start + self.time_window_seconds

            # Binary search for window boundaries (entries are sorted)
            lo = self._bisect_left(entries, t_start)
            hi = self._bisect_right(entries, t_end)

            window_entries = entries[lo:hi]
            window_features = features[lo:hi]

            if window_entries:
                # Pad to window_size if needed
                if self.pad_incomplete and len(window_features) < self.window_size:
                    window_features = _pad_features(
                        window_features, self.window_size, self.feature_dim
                    )
                elif len(window_features) > self.window_size:
                    # Truncate to most recent entries
                    window_entries = window_entries[-self.window_size:]
                    window_features = window_features[-self.window_size:]

                service_counts, dominant, errors, warnings = _compute_window_metadata(
                    window_entries
                )

                window = LogWindow(
                    window_id=window_id,
                    entries=window_entries,
                    features=window_features,
                    start_time=t_start,
                    end_time=t_end,
                    service_counts=service_counts,
                    dominant_service=dominant,
                    error_count=errors,
                    warning_count=warnings,
                )
                windows.append(window)
                window_id += 1

            t_start += self.time_window_stride_seconds

        logger.info(
            "Built %d time-based windows (duration=%.1fs, stride=%.1fs) from %d entries",
            len(windows),
            self.time_window_seconds,
            self.time_window_stride_seconds,
            n,
        )
        return windows

    @staticmethod
    def _bisect_left(entries: List[LogEntry], target_time: float) -> int:
        """Find leftmost entry with timestamp >= target_time."""
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if entries[mid].timestamp_unix < target_time:
                lo = mid + 1
            else:
                hi = mid
        return lo

    @staticmethod
    def _bisect_right(entries: List[LogEntry], target_time: float) -> int:
        """Find leftmost entry with timestamp > target_time."""
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if entries[mid].timestamp_unix <= target_time:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def build_service_windows(
        self,
        entries: List[LogEntry],
        features: List[List[float]],
    ) -> Dict[str, List[LogWindow]]:
        """Build separate window sequences per service.

        Useful for service-level anomaly detection (LSTM Autoencoder, VAE).

        Args:
            entries: Sorted entries (all services intermixed).
            features: Aligned feature matrix.

        Returns:
            Dict mapping service name → list of LogWindows for that service.
        """
        # Group by service
        service_entries: Dict[str, List[Tuple[LogEntry, List[float]]]] = {
            s: [] for s in SERVICES
        }
        for entry, feat in zip(entries, features):
            if entry.service in service_entries:
                service_entries[entry.service].append((entry, feat))

        result: Dict[str, List[LogWindow]] = {}
        for service, pairs in service_entries.items():
            if not pairs:
                result[service] = []
                continue
            svc_entries = [p[0] for p in pairs]
            svc_features = [p[1] for p in pairs]
            result[service] = self.build(svc_entries, svc_features)

        return result

"""
PyTorch-compatible dataset and end-to-end pipeline orchestrator.

Provides:
  - ``LogDataset``: A ``torch.utils.data.Dataset`` wrapping labeled windows
    for use with ``DataLoader``.
  - ``create_dataloaders()``: Convenience function to run the full pipeline
    (parse → template → featurize → window → label → split → wrap) and
    return train/val/test ``DataLoader`` objects ready for training.
  - ``build_pipeline()``: Run the full pipeline and return raw results
    without PyTorch wrapping (useful when torch is unavailable).

Usage::

    # With PyTorch
    train_loader, val_loader, test_loader = create_dataloaders()

    for batch in train_loader:
        features = batch["features"]       # (B, seq_len, feat_dim)
        severity = batch["severity_label"] # (B,)
        fault    = batch["fault_label"]    # (B,)

    # Without PyTorch
    result = build_pipeline()
    print(result.summary())
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from nn_pipeline.config import (
    FAULT_TYPES,
    LOG_LEVELS,
    RAW_LOGS_PATH,
    data_config,
)
from nn_pipeline.data.feature_engineer import FeatureEngineer
from nn_pipeline.data.labeler import LabelResult, WindowLabeler
from nn_pipeline.data.log_parser import LogEntry, parse_file
from nn_pipeline.data.template_extractor import TemplateExtractor, TemplateResult
from nn_pipeline.data.window_builder import LogWindow, WindowBuilder, WindowStrategy

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Lazy torch import
# ─────────────────────────────────────────────────────────────────────

_torch_available: Optional[bool] = None


def _check_torch() -> bool:
    """Check if torch is importable; cache the result."""
    global _torch_available
    if _torch_available is None:
        try:
            import torch  # noqa: F401
            _torch_available = True
        except ImportError:
            _torch_available = False
            logger.warning(
                "PyTorch not installed. LogDataset and create_dataloaders() "
                "will be unavailable. Install with: pip install torch"
            )
    return _torch_available


# ─────────────────────────────────────────────────────────────────────
# Pipeline result container
# ─────────────────────────────────────────────────────────────────────


@dataclass
class PipelineResult:
    """Container for the full pipeline output (no PyTorch dependency).

    Attributes:
        entries: All parsed LogEntry objects (sorted by timestamp).
        template_results: TemplateResult for each entry.
        features: Feature matrix, shape ``(N, feature_dim)``.
        windows: List of LogWindow objects.
        labels: LabelResult for each window.
        train_indices: Indices into ``windows`` for the training set.
        val_indices: Indices into ``windows`` for the validation set.
        test_indices: Indices into ``windows`` for the test set.
        template_extractor: The fitted TemplateExtractor (for reuse).
    """
    entries: List[LogEntry]
    template_results: List[TemplateResult]
    features: List[List[float]]
    windows: List[LogWindow]
    labels: List[LabelResult]
    train_indices: List[int]
    val_indices: List[int]
    test_indices: List[int]
    template_extractor: TemplateExtractor

    def summary(self) -> str:
        """Return a human-readable summary of the pipeline results."""
        fault_dist: Dict[str, int] = {ft: 0 for ft in FAULT_TYPES}
        sev_dist: Dict[str, int] = {lvl: 0 for lvl in LOG_LEVELS}
        for lbl in self.labels:
            fault_dist[lbl.fault_name] += 1
            sev_dist[lbl.severity_name] += 1

        lines = [
            "═══ Pipeline Summary ═══",
            f"  Parsed entries:      {len(self.entries):,}",
            f"  Unique templates:    {self.template_extractor.get_template_count()}",
            f"  Feature dim:         {len(self.features[0]) if self.features else 0}",
            f"  Windows:             {len(self.windows):,}",
            f"  Train / Val / Test:  {len(self.train_indices)} / "
            f"{len(self.val_indices)} / {len(self.test_indices)}",
            "",
            "  Severity distribution:",
        ]
        for lvl, cnt in sev_dist.items():
            lines.append(f"    {lvl:>8}: {cnt:>6}")

        lines.append("")
        lines.append("  Fault distribution:")
        for ft, cnt in fault_dist.items():
            lines.append(f"    {ft:>20}: {cnt:>6}")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Train/val/test splitting
# ─────────────────────────────────────────────────────────────────────


def _split_indices(
    n: int,
    train_ratio: float,
    val_ratio: float,
    test_ratio: float,
) -> Tuple[List[int], List[int], List[int]]:
    """Split indices chronologically (no shuffling — temporal order preserved).

    Args:
        n: Total number of items.
        train_ratio: Fraction for training.
        val_ratio: Fraction for validation.
        test_ratio: Fraction for testing.

    Returns:
        (train_indices, val_indices, test_indices)
    """
    train_end = int(math.floor(n * train_ratio))
    val_end = train_end + int(math.floor(n * val_ratio))

    train_indices = list(range(0, train_end))
    val_indices = list(range(train_end, val_end))
    test_indices = list(range(val_end, n))

    return train_indices, val_indices, test_indices


# ─────────────────────────────────────────────────────────────────────
# LogDataset (PyTorch)
# ─────────────────────────────────────────────────────────────────────


class LogDataset:
    """PyTorch Dataset wrapping labeled log windows.

    Each item is a dict with:
      - ``features``: Tensor of shape ``(seq_len, feature_dim)``
      - ``severity_label``: int tensor (scalar)
      - ``fault_label``: int tensor (scalar)
      - ``is_anomalous``: int tensor (0 or 1)
      - ``window_id``: int

    Requires PyTorch to be installed.
    """

    def __init__(
        self,
        windows: List[LogWindow],
        labels: List[LabelResult],
        indices: Optional[List[int]] = None,
    ) -> None:
        """Initialize the dataset.

        Args:
            windows: All windows from the pipeline.
            labels: All labels from the pipeline (aligned with windows).
            indices: Subset indices to use (e.g., train split).
                     If None, uses all windows.
        """
        if not _check_torch():
            raise ImportError(
                "PyTorch is required for LogDataset. "
                "Install with: pip install torch"
            )

        self._indices = indices if indices is not None else list(range(len(windows)))
        self._windows = windows
        self._labels = labels

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, idx: int) -> Dict[str, Any]:
        import torch

        real_idx = self._indices[idx]
        window = self._windows[real_idx]
        label = self._labels[real_idx]

        features = torch.tensor(window.features, dtype=torch.float32)
        severity = torch.tensor(label.severity_label, dtype=torch.long)
        fault = torch.tensor(label.fault_label, dtype=torch.long)
        anomalous = torch.tensor(int(label.is_anomalous), dtype=torch.long)

        return {
            "features": features,
            "severity_label": severity,
            "fault_label": fault,
            "is_anomalous": anomalous,
            "window_id": window.window_id,
        }


# ─────────────────────────────────────────────────────────────────────
# Full pipeline builder (no torch required)
# ─────────────────────────────────────────────────────────────────────


def build_pipeline(
    filepath: Optional[Path] = None,
    *,
    max_lines: Optional[int] = None,
    window_strategy: WindowStrategy = WindowStrategy.COUNT,
    error_rate_threshold: float = 0.1,
) -> PipelineResult:
    """Run the complete data pipeline end-to-end (no PyTorch required).

    Steps:
      1. Parse raw logs → ``LogEntry`` list
      2. Extract templates → ``TemplateResult`` per entry
      3. Compute features → per-entry feature vectors
      4. Build sliding windows → ``LogWindow`` list
      5. Label windows → ``LabelResult`` per window
      6. Split into train/val/test

    Args:
        filepath: Path to raw log file (defaults to config).
        max_lines: Limit number of raw lines to parse (for testing).
        window_strategy: COUNT or TIME-based windowing.
        error_rate_threshold: Threshold for fault detection.

    Returns:
        ``PipelineResult`` containing all intermediate and final outputs.
    """
    filepath = filepath or RAW_LOGS_PATH

    # 1. Parse
    logger.info("Step 1/6: Parsing raw logs...")
    entries = parse_file(filepath, max_lines=max_lines)
    logger.info("  → %d entries parsed", len(entries))

    if not entries:
        logger.warning("No entries parsed — returning empty pipeline result.")
        extractor = TemplateExtractor()
        return PipelineResult(
            entries=[],
            template_results=[],
            features=[],
            windows=[],
            labels=[],
            train_indices=[],
            val_indices=[],
            test_indices=[],
            template_extractor=extractor,
        )

    # 2. Template extraction
    logger.info("Step 2/6: Extracting templates...")
    extractor = TemplateExtractor()
    template_results = [extractor.extract(e.message) for e in entries]
    logger.info(
        "  → %d unique templates identified",
        extractor.get_template_count(),
    )

    # 3. Feature engineering
    logger.info("Step 3/6: Computing features...")
    engineer = FeatureEngineer()
    features = engineer.transform_batch(entries, template_results)
    logger.info("  → Feature matrix: %d × %d", len(features), engineer.feature_dim)

    # 4. Windowing
    logger.info("Step 4/6: Building windows...")
    builder = WindowBuilder(strategy=window_strategy)
    windows = builder.build(entries, features)
    logger.info("  → %d windows built", len(windows))

    # 5. Labeling
    logger.info("Step 5/6: Labeling windows...")
    labeler = WindowLabeler(error_rate_threshold=error_rate_threshold)
    labels = labeler.label_batch(windows)

    # 6. Split
    logger.info("Step 6/6: Splitting into train/val/test...")
    train_idx, val_idx, test_idx = _split_indices(
        len(windows),
        data_config.train_ratio,
        data_config.val_ratio,
        data_config.test_ratio,
    )
    logger.info(
        "  → Train: %d, Val: %d, Test: %d",
        len(train_idx),
        len(val_idx),
        len(test_idx),
    )

    # Save templates
    extractor.save()

    result = PipelineResult(
        entries=entries,
        template_results=template_results,
        features=features,
        windows=windows,
        labels=labels,
        train_indices=train_idx,
        val_indices=val_idx,
        test_indices=test_idx,
        template_extractor=extractor,
    )

    logger.info("\n%s", result.summary())
    return result


# ─────────────────────────────────────────────────────────────────────
# DataLoader factory (requires PyTorch)
# ─────────────────────────────────────────────────────────────────────


def create_dataloaders(
    filepath: Optional[Path] = None,
    *,
    max_lines: Optional[int] = None,
    window_strategy: WindowStrategy = WindowStrategy.COUNT,
    batch_size: Optional[int] = None,
    num_workers: Optional[int] = None,
    pin_memory: Optional[bool] = None,
) -> Tuple[Any, Any, Any]:
    """Run the full pipeline and return PyTorch DataLoaders.

    Args:
        filepath: Path to raw log file.
        max_lines: Limit lines to parse (for testing).
        window_strategy: Windowing strategy.
        batch_size: Batch size (default from config).
        num_workers: DataLoader workers (default from config).
        pin_memory: Pin memory for GPU transfer (default from config).

    Returns:
        (train_loader, val_loader, test_loader) — three DataLoader objects.

    Raises:
        ImportError: If PyTorch is not installed.
    """
    if not _check_torch():
        raise ImportError(
            "PyTorch is required for create_dataloaders(). "
            "Install with: pip install torch"
        )

    import torch.utils.data as torch_data

    batch_size = batch_size or data_config.batch_size
    num_workers = num_workers or data_config.num_workers
    pin_memory = pin_memory if pin_memory is not None else data_config.pin_memory

    # Run pipeline
    result = build_pipeline(
        filepath,
        max_lines=max_lines,
        window_strategy=window_strategy,
    )

    if not result.windows:
        raise ValueError("Pipeline produced no windows — cannot create DataLoaders.")

    # Create datasets
    train_ds = LogDataset(result.windows, result.labels, result.train_indices)
    val_ds = LogDataset(result.windows, result.labels, result.val_indices)
    test_ds = LogDataset(result.windows, result.labels, result.test_indices)

    # Create loaders
    train_loader = torch_data.DataLoader(
        train_ds,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=pin_memory,
        drop_last=True,
    )
    val_loader = torch_data.DataLoader(
        val_ds,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=pin_memory,
    )
    test_loader = torch_data.DataLoader(
        test_ds,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=pin_memory,
    )

    logger.info(
        "Created DataLoaders — Train: %d batches, Val: %d batches, Test: %d batches",
        len(train_loader),
        len(val_loader),
        len(test_loader),
    )

    return train_loader, val_loader, test_loader

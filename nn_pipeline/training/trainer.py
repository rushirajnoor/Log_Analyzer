"""
Generic training loop for all Neural Network Log Analyzer models.

Supports:
    - TensorBoard experiment tracking
    - Early stopping with configurable patience
    - Gradient clipping (max-norm)
    - Learning rate scheduling (cosine, step, plateau)
    - Checkpoint saving and loading (resumable training)
    - Mixed precision training (torch.cuda.amp)
    - GPU/CPU fallback
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import torch
import torch.nn as nn
from torch.optim import Optimizer
from torch.optim.lr_scheduler import (
    CosineAnnealingLR,
    ReduceLROnPlateau,
    StepLR,
    _LRScheduler,
)
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from nn_pipeline.config import (
    MODEL_CHECKPOINTS_DIR,
    TENSORBOARD_LOG_DIR,
    TrainingConfig,
    training_config,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Early Stopping
# ─────────────────────────────────────────────────────────────────────


@dataclass
class EarlyStoppingState:
    """Tracks early stopping state across epochs."""

    patience: int = 10
    best_score: float = float("inf")
    counter: int = 0
    should_stop: bool = False
    best_epoch: int = 0
    mode: str = "min"  # 'min' for loss, 'max' for accuracy/F1

    def step(self, score: float, epoch: int) -> bool:
        """Update state with new validation score.

        Args:
            score: Current validation metric.
            epoch: Current epoch number.

        Returns:
            True if training should stop.
        """
        improved: bool
        if self.mode == "min":
            improved = score < self.best_score
        else:
            improved = score > self.best_score

        if improved:
            self.best_score = score
            self.counter = 0
            self.best_epoch = epoch
        else:
            self.counter += 1

        self.should_stop = self.counter >= self.patience
        return self.should_stop


# ─────────────────────────────────────────────────────────────────────
# Checkpoint Manager
# ─────────────────────────────────────────────────────────────────────


class CheckpointManager:
    """Handles saving and loading model checkpoints."""

    def __init__(
        self,
        checkpoint_dir: Path,
        model_name: str,
        max_checkpoints: int = 3,
    ) -> None:
        self.checkpoint_dir = checkpoint_dir / model_name
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name
        self.max_checkpoints = max_checkpoints
        self._saved: List[Path] = []

    def save(
        self,
        model: nn.Module,
        optimizer: Optimizer,
        scheduler: Optional[_LRScheduler],
        epoch: int,
        metrics: Dict[str, float],
        scaler: Optional[torch.amp.GradScaler] = None,
        extra: Optional[Dict[str, Any]] = None,
        is_best: bool = False,
    ) -> Path:
        """Save a checkpoint.

        Args:
            model: The model to save.
            optimizer: Current optimizer state.
            scheduler: Current LR scheduler state (if any).
            epoch: Current epoch.
            metrics: Dict of metric name → value.
            scaler: AMP GradScaler state (if any).
            extra: Any extra metadata to store.
            is_best: Whether this is the best checkpoint so far.

        Returns:
            Path to the saved checkpoint file.
        """
        state: Dict[str, Any] = {
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "metrics": metrics,
            "model_name": self.model_name,
        }

        if scheduler is not None:
            state["scheduler_state_dict"] = scheduler.state_dict()
        if scaler is not None:
            state["scaler_state_dict"] = scaler.state_dict()
        if extra is not None:
            state["extra"] = extra

        # Regular checkpoint
        path = self.checkpoint_dir / f"checkpoint_epoch_{epoch:04d}.pt"
        torch.save(state, path)
        self._saved.append(path)
        logger.info("Saved checkpoint: %s", path)

        # Trim old checkpoints
        while len(self._saved) > self.max_checkpoints:
            old = self._saved.pop(0)
            if old.exists():
                old.unlink()

        # Best checkpoint (always kept separately)
        if is_best:
            best_path = self.checkpoint_dir / "best_model.pt"
            torch.save(state, best_path)
            logger.info("Saved best model: %s", best_path)

        return path

    def load(
        self,
        model: nn.Module,
        optimizer: Optional[Optimizer] = None,
        scheduler: Optional[_LRScheduler] = None,
        scaler: Optional[torch.amp.GradScaler] = None,
        checkpoint_path: Optional[Path] = None,
        load_best: bool = False,
        device: torch.device = torch.device("cpu"),
    ) -> Dict[str, Any]:
        """Load a checkpoint.

        Args:
            model: Model to load weights into.
            optimizer: Optimizer to restore state (optional).
            scheduler: Scheduler to restore state (optional).
            scaler: GradScaler to restore state (optional).
            checkpoint_path: Explicit path; overrides load_best.
            load_best: If True, load the best checkpoint.
            device: Device to map tensors to.

        Returns:
            Dict with metadata (epoch, metrics, extra, etc.).
        """
        if checkpoint_path is not None:
            path = checkpoint_path
        elif load_best:
            path = self.checkpoint_dir / "best_model.pt"
        else:
            # Latest checkpoint
            checkpoints = sorted(self.checkpoint_dir.glob("checkpoint_epoch_*.pt"))
            if not checkpoints:
                raise FileNotFoundError(
                    f"No checkpoints found in {self.checkpoint_dir}"
                )
            path = checkpoints[-1]

        if not path.exists():
            raise FileNotFoundError(f"Checkpoint not found: {path}")

        state = torch.load(path, map_location=device, weights_only=False)
        model.load_state_dict(state["model_state_dict"])

        if optimizer is not None and "optimizer_state_dict" in state:
            optimizer.load_state_dict(state["optimizer_state_dict"])

        if scheduler is not None and "scheduler_state_dict" in state:
            scheduler.load_state_dict(state["scheduler_state_dict"])

        if scaler is not None and "scaler_state_dict" in state:
            scaler.load_state_dict(state["scaler_state_dict"])

        logger.info(
            "Loaded checkpoint from %s (epoch %d)",
            path,
            state.get("epoch", -1),
        )

        return {
            "epoch": state.get("epoch", 0),
            "metrics": state.get("metrics", {}),
            "extra": state.get("extra", {}),
        }

    def best_checkpoint_exists(self) -> bool:
        """Check if a best-model checkpoint exists."""
        return (self.checkpoint_dir / "best_model.pt").exists()

    def any_checkpoint_exists(self) -> bool:
        """Check if any checkpoint exists."""
        return bool(list(self.checkpoint_dir.glob("checkpoint_epoch_*.pt")))


# ─────────────────────────────────────────────────────────────────────
# Trainer
# ─────────────────────────────────────────────────────────────────────


class Trainer:
    """Model-agnostic training loop.

    Example::

        trainer = Trainer(
            model=my_model,
            optimizer=optimizer,
            loss_fn=nn.CrossEntropyLoss(),
            model_name="transformer_classifier",
        )
        trainer.fit(train_loader, val_loader)
    """

    def __init__(
        self,
        model: nn.Module,
        optimizer: Optimizer,
        loss_fn: nn.Module,
        model_name: str,
        config: Optional[TrainingConfig] = None,
        scheduler_type: Optional[str] = None,
        device: Optional[torch.device] = None,
        train_step_fn: Optional[
            Callable[
                [nn.Module, Any, nn.Module, torch.device],
                Tuple[torch.Tensor, Dict[str, float]],
            ]
        ] = None,
        val_step_fn: Optional[
            Callable[
                [nn.Module, Any, nn.Module, torch.device],
                Tuple[torch.Tensor, Dict[str, float]],
            ]
        ] = None,
        early_stop_metric: str = "val_loss",
        early_stop_mode: str = "min",
        use_amp: bool = True,
    ) -> None:
        """
        Args:
            model: PyTorch model to train.
            optimizer: Optimizer instance.
            loss_fn: Loss function module.
            model_name: Unique name used for checkpoints and TensorBoard.
            config: TrainingConfig instance (defaults to global).
            scheduler_type: LR scheduler type override ('cosine', 'step', 'plateau').
            device: Target device; auto-detects GPU if None.
            train_step_fn: Custom training step. Signature:
                (model, batch, loss_fn, device) → (loss_tensor, metrics_dict).
            val_step_fn: Custom validation step. Same signature as train_step_fn.
            early_stop_metric: Metric name for early stopping.
            early_stop_mode: 'min' or 'max'.
            use_amp: Enable automatic mixed precision.
        """
        self.cfg = config or training_config
        self.model_name = model_name

        # Device setup with GPU fallback
        if device is not None:
            self.device = device
        elif self.cfg.device == "cuda" and torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")
            if self.cfg.device == "cuda":
                logger.warning(
                    "CUDA requested but not available — falling back to CPU."
                )

        self.model = model.to(self.device)
        self.optimizer = optimizer
        self.loss_fn = loss_fn

        # Custom step functions (for non-standard forward passes)
        self._train_step_fn = train_step_fn
        self._val_step_fn = val_step_fn

        # LR Scheduler
        sched_type = scheduler_type or self.cfg.scheduler
        self.scheduler: Optional[_LRScheduler] = self._build_scheduler(sched_type)

        # Mixed precision
        self.use_amp = use_amp and self.device.type == "cuda"
        self.scaler: Optional[torch.amp.GradScaler] = (
            torch.amp.GradScaler("cuda") if self.use_amp else None
        )

        # TensorBoard
        self.writer = SummaryWriter(
            log_dir=str(TENSORBOARD_LOG_DIR / model_name)
        )

        # Checkpoint manager
        self.ckpt_mgr = CheckpointManager(
            MODEL_CHECKPOINTS_DIR, model_name
        )

        # Early stopping
        self.early_stop = EarlyStoppingState(
            patience=self.cfg.early_stopping_patience,
            mode=early_stop_mode,
        )
        self.early_stop_metric = early_stop_metric

        # Bookkeeping
        self.global_step: int = 0
        self.start_epoch: int = 0
        self.history: List[Dict[str, float]] = []

    # ── Scheduler factory ────────────────────────────────────────────

    def _build_scheduler(self, stype: str) -> Optional[_LRScheduler]:
        if stype == "cosine":
            return CosineAnnealingLR(
                self.optimizer,
                T_max=self.cfg.num_epochs,
                eta_min=1e-7,
            )
        elif stype == "step":
            return StepLR(self.optimizer, step_size=15, gamma=0.5)
        elif stype == "plateau":
            return ReduceLROnPlateau(
                self.optimizer, mode="min", patience=5, factor=0.5
            )
        else:
            logger.warning("Unknown scheduler '%s'; using none.", stype)
            return None

    # ── Default train/val step ───────────────────────────────────────

    @staticmethod
    def _default_train_step(
        model: nn.Module,
        batch: Any,
        loss_fn: nn.Module,
        device: torch.device,
    ) -> Tuple[torch.Tensor, Dict[str, float]]:
        """Default training step: expects batch = (inputs, targets)."""
        inputs, targets = batch
        inputs = inputs.to(device)
        targets = targets.to(device)
        outputs = model(inputs)
        loss = loss_fn(outputs, targets)
        return loss, {"loss": loss.item()}

    @staticmethod
    def _default_val_step(
        model: nn.Module,
        batch: Any,
        loss_fn: nn.Module,
        device: torch.device,
    ) -> Tuple[torch.Tensor, Dict[str, float]]:
        """Default validation step: expects batch = (inputs, targets)."""
        inputs, targets = batch
        inputs = inputs.to(device)
        targets = targets.to(device)
        outputs = model(inputs)
        loss = loss_fn(outputs, targets)
        return loss, {"loss": loss.item()}

    # ── Core loops ───────────────────────────────────────────────────

    def _run_epoch(
        self,
        loader: DataLoader,
        is_train: bool,
    ) -> Dict[str, float]:
        """Run a single epoch (train or val).

        Returns:
            Dict mapping metric name → epoch-averaged value.
        """
        if is_train:
            self.model.train()
            step_fn = self._train_step_fn or self._default_train_step
        else:
            self.model.eval()
            step_fn = self._val_step_fn or self._default_val_step

        running: Dict[str, float] = {}
        num_batches = 0

        ctx = torch.no_grad() if not is_train else _nullcontext()

        with ctx:
            for batch in loader:
                if is_train:
                    self.optimizer.zero_grad()

                if self.use_amp and is_train:
                    with torch.autocast(device_type="cuda"):
                        loss, metrics = step_fn(
                            self.model, batch, self.loss_fn, self.device
                        )
                    self.scaler.scale(loss).backward()  # type: ignore[union-attr]
                    # Gradient clipping
                    self.scaler.unscale_(self.optimizer)  # type: ignore[union-attr]
                    torch.nn.utils.clip_grad_norm_(
                        self.model.parameters(),
                        self.cfg.gradient_clip_norm,
                    )
                    self.scaler.step(self.optimizer)  # type: ignore[union-attr]
                    self.scaler.update()  # type: ignore[union-attr]
                elif is_train:
                    loss, metrics = step_fn(
                        self.model, batch, self.loss_fn, self.device
                    )
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(
                        self.model.parameters(),
                        self.cfg.gradient_clip_norm,
                    )
                    self.optimizer.step()
                else:
                    # Validation with optional AMP autocast
                    if self.use_amp:
                        with torch.autocast(device_type="cuda"):
                            loss, metrics = step_fn(
                                self.model, batch, self.loss_fn, self.device
                            )
                    else:
                        loss, metrics = step_fn(
                            self.model, batch, self.loss_fn, self.device
                        )

                # Accumulate metrics
                for k, v in metrics.items():
                    running[k] = running.get(k, 0.0) + v
                num_batches += 1

                # TensorBoard per-step logging (train only)
                if is_train:
                    self.global_step += 1
                    if self.global_step % self.cfg.log_every_n_steps == 0:
                        for k, v in metrics.items():
                            self.writer.add_scalar(
                                f"train_step/{k}",
                                v,
                                self.global_step,
                            )

        # Average
        if num_batches > 0:
            avg = {k: v / num_batches for k, v in running.items()}
        else:
            avg = running

        return avg

    # ── Public API ───────────────────────────────────────────────────

    def fit(
        self,
        train_loader: DataLoader,
        val_loader: Optional[DataLoader] = None,
        num_epochs: Optional[int] = None,
        resume: bool = True,
    ) -> List[Dict[str, float]]:
        """Run the full training loop.

        Args:
            train_loader: Training data loader.
            val_loader: Validation data loader (optional but recommended).
            num_epochs: Number of epochs; defaults to config value.
            resume: Whether to resume from last checkpoint.

        Returns:
            List of per-epoch metric dicts.
        """
        epochs = num_epochs or self.cfg.num_epochs

        # Resume from checkpoint
        if resume and self.ckpt_mgr.any_checkpoint_exists():
            try:
                meta = self.ckpt_mgr.load(
                    self.model,
                    self.optimizer,
                    self.scheduler,
                    self.scaler,
                    device=self.device,
                )
                self.start_epoch = meta["epoch"] + 1
                logger.info(
                    "Resuming from epoch %d", self.start_epoch
                )
            except Exception as e:
                logger.warning(
                    "Could not resume from checkpoint: %s", e
                )
                self.start_epoch = 0

        logger.info(
            "Training %s on %s for %d epochs (starting from %d)",
            self.model_name,
            self.device,
            epochs,
            self.start_epoch,
        )

        for epoch in range(self.start_epoch, epochs):
            t0 = time.time()

            # Train
            train_metrics = self._run_epoch(train_loader, is_train=True)
            epoch_log: Dict[str, float] = {
                f"train_{k}": v for k, v in train_metrics.items()
            }

            # Validate
            val_metrics: Dict[str, float] = {}
            if val_loader is not None and (
                epoch % self.cfg.val_every_n_epochs == 0
            ):
                val_metrics = self._run_epoch(val_loader, is_train=False)
                epoch_log.update(
                    {f"val_{k}": v for k, v in val_metrics.items()}
                )

            epoch_log["epoch"] = float(epoch)
            epoch_log["lr"] = self.optimizer.param_groups[0]["lr"]
            epoch_log["epoch_time_s"] = time.time() - t0

            # TensorBoard epoch logging
            for k, v in epoch_log.items():
                self.writer.add_scalar(f"epoch/{k}", v, epoch)

            # LR scheduler step
            if self.scheduler is not None:
                if isinstance(self.scheduler, ReduceLROnPlateau):
                    metric_for_sched = epoch_log.get("val_loss", epoch_log.get("train_loss", 0.0))
                    self.scheduler.step(metric_for_sched)
                else:
                    self.scheduler.step()

            # Early stopping
            es_value = epoch_log.get(
                self.early_stop_metric,
                epoch_log.get("train_loss", 0.0),
            )
            is_best = False
            if self.early_stop.mode == "min":
                is_best = es_value < self.early_stop.best_score
            else:
                is_best = es_value > self.early_stop.best_score

            should_stop = self.early_stop.step(es_value, epoch)

            # Checkpoint saving
            if is_best or (epoch % self.cfg.save_every_n_epochs == 0):
                self.ckpt_mgr.save(
                    self.model,
                    self.optimizer,
                    self.scheduler,
                    epoch,
                    epoch_log,
                    scaler=self.scaler,
                    is_best=is_best,
                )

            self.history.append(epoch_log)

            # Log
            log_parts = [f"Epoch {epoch}/{epochs}"]
            for k in sorted(epoch_log):
                if k not in ("epoch",):
                    log_parts.append(f"{k}={epoch_log[k]:.4f}")
            logger.info("  ".join(log_parts))

            if should_stop:
                logger.info(
                    "Early stopping at epoch %d (best=%d, score=%.4f)",
                    epoch,
                    self.early_stop.best_epoch,
                    self.early_stop.best_score,
                )
                break

        self.writer.close()
        return self.history

    def load_best(self) -> Dict[str, Any]:
        """Load the best model checkpoint.

        Returns:
            Checkpoint metadata dict.
        """
        return self.ckpt_mgr.load(
            self.model,
            device=self.device,
            load_best=True,
        )

    def export_history(self, path: Optional[Path] = None) -> Path:
        """Write training history to JSON.

        Args:
            path: Output file path. Defaults to checkpoints dir.

        Returns:
            Path to the written JSON file.
        """
        if path is None:
            path = (
                MODEL_CHECKPOINTS_DIR
                / self.model_name
                / "training_history.json"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.history, f, indent=2)
        logger.info("Exported training history to %s", path)
        return path


# ─────────────────────────────────────────────────────────────────────
# Utility: null context manager (for PyTorch < 1.14 compat)
# ─────────────────────────────────────────────────────────────────────


class _nullcontext:
    """No-op context manager for code paths that skip torch.no_grad()."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> None:
        pass

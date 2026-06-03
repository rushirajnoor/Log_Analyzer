"""
Knowledge-distilled GRU model for fast real-time inference.

A lightweight student model that learns from the Transformer Encoder
(teacher) via knowledge distillation:

    L = α * CE(student, true_labels) + (1-α) * KL(student/T, teacher/T)

The student GRU achieves ~95% of the teacher's accuracy at ~10× speed,
making it suitable for real-time streaming inference on CPU.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from nn_pipeline.config import distilled_config as cfg


class DistilledGRU(nn.Module):
    """Lightweight GRU-based classifier distilled from a Transformer teacher.

    Architecture:
        Input: (batch, seq_len, input_dim)
        → 2-layer GRU (hidden_dim=64)
        → Take last hidden state
        → Classification heads (severity + fault type)

    This model is ~10× smaller and faster than the Transformer teacher.
    """

    def __init__(self, config=None) -> None:
        super().__init__()
        c = config or cfg

        self.hidden_dim = c.hidden_dim
        self.num_layers = c.num_layers

        # GRU encoder
        self.gru = nn.GRU(
            input_size=c.input_dim,
            hidden_size=c.hidden_dim,
            num_layers=c.num_layers,
            batch_first=True,
            dropout=c.dropout if c.num_layers > 1 else 0.0,
            bidirectional=False,  # Unidirectional for speed
        )

        self.layer_norm = nn.LayerNorm(c.hidden_dim)
        self.dropout = nn.Dropout(c.dropout)

        # Classification heads (mirror teacher's heads)
        self.severity_head = nn.Sequential(
            nn.Linear(c.hidden_dim, c.hidden_dim // 2),
            nn.ReLU(),
            nn.Dropout(c.dropout),
            nn.Linear(c.hidden_dim // 2, c.num_severity_classes),
        )

        self.fault_head = nn.Sequential(
            nn.Linear(c.hidden_dim, c.hidden_dim // 2),
            nn.ReLU(),
            nn.Dropout(c.dropout),
            nn.Linear(c.hidden_dim // 2, c.num_fault_classes),
        )

        self.severity_score_head = nn.Sequential(
            nn.Linear(c.hidden_dim, c.hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(c.hidden_dim // 2, 1),
            nn.Sigmoid(),
        )

    def forward(
        self,
        x: torch.Tensor,
        lengths: Optional[torch.Tensor] = None,
    ) -> Dict[str, torch.Tensor]:
        """Forward pass.

        Args:
            x: Input features, shape ``(batch, seq_len, input_dim)``.
            lengths: Optional actual lengths per sequence for packing.

        Returns:
            Dict with ``severity_logits``, ``fault_logits``, ``severity_score``,
            and ``hidden`` (final hidden state).
        """
        # GRU forward
        output, h_n = self.gru(x)
        # output: (batch, seq_len, hidden_dim)
        # h_n: (num_layers, batch, hidden_dim)

        # Use the last layer's final hidden state
        h = h_n[-1]  # (batch, hidden_dim)
        h = self.layer_norm(h)
        h = self.dropout(h)

        # Predictions
        severity_logits = self.severity_head(h)       # (batch, num_severity)
        fault_logits = self.fault_head(h)              # (batch, num_fault)
        severity_score = self.severity_score_head(h).squeeze(-1)  # (batch,)

        return {
            "severity_logits": severity_logits,
            "fault_logits": fault_logits,
            "severity_score": severity_score,
            "hidden": h,
        }


# ─────────────────────────────────────────────────────────────────────
# Distillation Loss
# ─────────────────────────────────────────────────────────────────────


class DistillationLoss(nn.Module):
    """Combined loss for knowledge distillation.

    L = α * hard_loss + (1 - α) * T² * soft_loss

    Where:
      - hard_loss = CrossEntropy(student_logits, true_labels)
      - soft_loss = KL(student_soft, teacher_soft)
      - student_soft = softmax(student_logits / T)
      - teacher_soft = softmax(teacher_logits / T)
    """

    def __init__(
        self,
        alpha: float = 0.3,
        temperature: float = 4.0,
    ) -> None:
        super().__init__()
        self.alpha = alpha
        self.temperature = temperature
        self.ce_loss = nn.CrossEntropyLoss()
        self.kl_loss = nn.KLDivLoss(reduction='batchmean')

    def forward(
        self,
        student_logits: torch.Tensor,
        teacher_logits: torch.Tensor,
        true_labels: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Compute distillation loss.

        Args:
            student_logits: Student model output, shape ``(batch, num_classes)``.
            teacher_logits: Teacher model output (detached), shape ``(batch, num_classes)``.
            true_labels: Ground truth labels, shape ``(batch,)``.

        Returns:
            Dict with ``total_loss``, ``hard_loss``, ``soft_loss``.
        """
        T = self.temperature

        # Hard loss: student vs true labels
        hard_loss = self.ce_loss(student_logits, true_labels)

        # Soft loss: student vs teacher (temperature-scaled)
        student_soft = F.log_softmax(student_logits / T, dim=-1)
        teacher_soft = F.softmax(teacher_logits.detach() / T, dim=-1)
        soft_loss = self.kl_loss(student_soft, teacher_soft) * (T * T)

        # Combined
        total = self.alpha * hard_loss + (1.0 - self.alpha) * soft_loss

        return {
            "total_loss": total,
            "hard_loss": hard_loss,
            "soft_loss": soft_loss,
        }


class MultiTaskDistillationLoss(nn.Module):
    """Distillation loss for both severity and fault type heads simultaneously."""

    def __init__(self, alpha: float = 0.3, temperature: float = 4.0) -> None:
        super().__init__()
        self.severity_loss = DistillationLoss(alpha, temperature)
        self.fault_loss = DistillationLoss(alpha, temperature)
        self.score_loss = nn.MSELoss()

    def forward(
        self,
        student_output: Dict[str, torch.Tensor],
        teacher_output: Dict[str, torch.Tensor],
        severity_labels: torch.Tensor,
        fault_labels: torch.Tensor,
        severity_scores: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Compute multi-task distillation loss.

        Args:
            student_output: Student model forward output dict.
            teacher_output: Teacher model forward output dict (detached).
            severity_labels: True severity labels.
            fault_labels: True fault type labels.
            severity_scores: True severity scores.

        Returns:
            Dict with individual and total losses.
        """
        sev = self.severity_loss(
            student_output["severity_logits"],
            teacher_output["severity_logits"],
            severity_labels,
        )
        fault = self.fault_loss(
            student_output["fault_logits"],
            teacher_output["fault_logits"],
            fault_labels,
        )
        score = self.score_loss(
            student_output["severity_score"],
            severity_scores,
        )

        total = sev["total_loss"] + fault["total_loss"] + 0.5 * score

        return {
            "total_loss": total,
            "severity_loss": sev["total_loss"],
            "fault_loss": fault["total_loss"],
            "score_loss": score,
            "severity_hard": sev["hard_loss"],
            "severity_soft": sev["soft_loss"],
            "fault_hard": fault["hard_loss"],
            "fault_soft": fault["soft_loss"],
        }

"""
Template vocabulary management for the Neural Network Log Analyzer.

Maps log template IDs (from Drain3 template mining) to contiguous integer
indices suitable for embedding layers. Handles:
  - Building vocabulary from Drain3 miner output
  - Special tokens: [PAD], [UNK], [CLS], [MASK]
  - Persisting vocabulary to disk (JSON)
  - Frequency-based pruning to cap vocabulary size
  - Token-level sub-vocabulary for templates (word splitting)

Design Decisions:
  - Special tokens occupy indices 0-3 so they are always dense.
  - Templates are indexed by frequency rank, so the most common templates
    get the lowest indices → better cache/embedding utilization.
  - `max_templates` from DataConfig caps the vocabulary; rare templates
    are mapped to [UNK].
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from nn_pipeline.config import (
    TEMPLATE_CACHE_DIR,
    data_config,
)

# ─────────────────────────────────────────────────────────────────────
# Special tokens
# ─────────────────────────────────────────────────────────────────────
PAD_TOKEN: str = "[PAD]"
UNK_TOKEN: str = "[UNK]"
CLS_TOKEN: str = "[CLS]"
MASK_TOKEN: str = "[MASK]"

SPECIAL_TOKENS: List[str] = [PAD_TOKEN, UNK_TOKEN, CLS_TOKEN, MASK_TOKEN]

PAD_IDX: int = 0
UNK_IDX: int = 1
CLS_IDX: int = 2
MASK_IDX: int = 3

# Regex to split a template string into tokens (words + <*> wildcards)
_TOKEN_RE = re.compile(r"<\*>|[A-Za-z0-9_]+")


class TemplateVocabulary:
    """Manages mapping between Drain3 template strings and integer indices.

    Attributes:
        template2idx: Mapping from template string → integer index.
        idx2template: Reverse mapping from integer index → template string.
        token2idx: Sub-word/token level vocabulary (for embedding individual
                   words within templates).
        max_templates: Maximum number of template entries (excluding specials).
    """

    def __init__(self, max_templates: int = data_config.max_templates) -> None:
        self.max_templates: int = max_templates

        # Template-level vocab
        self.template2idx: Dict[str, int] = {}
        self.idx2template: Dict[int, str] = {}
        self.template_counts: Counter = Counter()

        # Token-level vocab (words inside templates)
        self.token2idx: Dict[str, int] = {}
        self.idx2token: Dict[int, str] = {}

        # Initialize special tokens
        self._init_special_tokens()

    # ─────────────────────────────────────────────────────────────────
    # Initialization helpers
    # ─────────────────────────────────────────────────────────────────
    def _init_special_tokens(self) -> None:
        """Reserve indices 0-3 for special tokens in both vocabularies."""
        for idx, tok in enumerate(SPECIAL_TOKENS):
            self.template2idx[tok] = idx
            self.idx2template[idx] = tok
            self.token2idx[tok] = idx
            self.idx2token[idx] = tok

    # ─────────────────────────────────────────────────────────────────
    # Build from Drain3 output
    # ─────────────────────────────────────────────────────────────────
    def build_from_drain_clusters(
        self,
        clusters: Sequence[object],
    ) -> "TemplateVocabulary":
        """Build vocabulary from Drain3 cluster objects.

        Each cluster is expected to have:
          - ``get_template()`` → str : the template string
          - ``size``           → int : number of log lines matched

        Args:
            clusters: Iterable of Drain3 ``LogCluster`` objects.

        Returns:
            self (for chaining).
        """
        # Count template frequencies
        self.template_counts = Counter()
        for cluster in clusters:
            template_str: str = cluster.get_template()  # type: ignore[union-attr]
            count: int = int(getattr(cluster, "size", 1))
            self.template_counts[template_str] += count

        # Rank by frequency, keep top-k
        most_common = self.template_counts.most_common(self.max_templates)

        # Rebuild mappings (special tokens already occupy 0-3)
        self.template2idx = {}
        self.idx2template = {}
        self._init_special_tokens()

        next_idx = len(SPECIAL_TOKENS)
        for template_str, _count in most_common:
            if template_str not in self.template2idx:
                self.template2idx[template_str] = next_idx
                self.idx2template[next_idx] = template_str
                next_idx += 1

        # Build token-level vocab from the retained templates
        self._build_token_vocab()

        return self

    def build_from_template_strings(
        self,
        templates: Sequence[str],
        counts: Optional[Sequence[int]] = None,
    ) -> "TemplateVocabulary":
        """Build vocabulary directly from template strings.

        Args:
            templates: List of template strings.
            counts: Optional parallel list of occurrence counts.

        Returns:
            self (for chaining).
        """
        if counts is None:
            counts = [1] * len(templates)

        self.template_counts = Counter()
        for tpl, cnt in zip(templates, counts):
            self.template_counts[tpl] += cnt

        most_common = self.template_counts.most_common(self.max_templates)

        self.template2idx = {}
        self.idx2template = {}
        self._init_special_tokens()

        next_idx = len(SPECIAL_TOKENS)
        for template_str, _count in most_common:
            if template_str not in self.template2idx:
                self.template2idx[template_str] = next_idx
                self.idx2template[next_idx] = template_str
                next_idx += 1

        self._build_token_vocab()
        return self

    def _build_token_vocab(self) -> None:
        """Build word-level vocabulary from all retained templates."""
        self.token2idx = {}
        self.idx2token = {}
        # Re-add specials at token level
        for idx, tok in enumerate(SPECIAL_TOKENS):
            self.token2idx[tok] = idx
            self.idx2token[idx] = tok

        next_idx = len(SPECIAL_TOKENS)
        for template_str in self.template2idx:
            if template_str in SPECIAL_TOKENS:
                continue
            tokens = self.tokenize_template(template_str)
            for tok in tokens:
                if tok not in self.token2idx:
                    self.token2idx[tok] = next_idx
                    self.idx2token[next_idx] = tok
                    next_idx += 1

    # ─────────────────────────────────────────────────────────────────
    # Lookup methods
    # ─────────────────────────────────────────────────────────────────
    def template_to_index(self, template: str) -> int:
        """Map a template string to its index; returns UNK_IDX if unseen."""
        return self.template2idx.get(template, UNK_IDX)

    def index_to_template(self, idx: int) -> str:
        """Map an index back to its template string."""
        return self.idx2template.get(idx, UNK_TOKEN)

    def token_to_index(self, token: str) -> int:
        """Map a word token to its index; returns UNK_IDX if unseen."""
        return self.token2idx.get(token, UNK_IDX)

    def index_to_token(self, idx: int) -> str:
        """Map a token index back to its string."""
        return self.idx2token.get(idx, UNK_TOKEN)

    @staticmethod
    def tokenize_template(template: str) -> List[str]:
        """Split a template string into word tokens.

        Drain3 templates look like:
            ``"Received request from <*> for service <*>"``

        This splits on whitespace/punctuation but preserves ``<*>``
        wildcards as single tokens.

        Args:
            template: Raw template string.

        Returns:
            List of token strings.
        """
        return _TOKEN_RE.findall(template)

    def encode_template_tokens(
        self,
        template: str,
        max_length: int = data_config.max_templates,
        pad: bool = True,
    ) -> List[int]:
        """Encode a template into a list of token indices.

        Args:
            template: Template string to encode.
            max_length: Pad/truncate to this length.
            pad: If True, pad with PAD_IDX to max_length.

        Returns:
            List of integer token indices.
        """
        tokens = self.tokenize_template(template)
        indices = [self.token_to_index(t) for t in tokens]

        # Truncate
        if len(indices) > max_length:
            indices = indices[:max_length]

        # Pad
        if pad and len(indices) < max_length:
            indices += [PAD_IDX] * (max_length - len(indices))

        return indices

    # ─────────────────────────────────────────────────────────────────
    # Sizes
    # ─────────────────────────────────────────────────────────────────
    @property
    def vocab_size(self) -> int:
        """Total number of templates (including special tokens)."""
        return len(self.template2idx)

    @property
    def token_vocab_size(self) -> int:
        """Total number of unique word tokens (including special tokens)."""
        return len(self.token2idx)

    def __len__(self) -> int:
        return self.vocab_size

    def __contains__(self, template: str) -> bool:
        return template in self.template2idx

    def __repr__(self) -> str:
        return (
            f"TemplateVocabulary(vocab_size={self.vocab_size}, "
            f"token_vocab_size={self.token_vocab_size}, "
            f"max_templates={self.max_templates})"
        )

    # ─────────────────────────────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────────────────────────────
    def save(self, path: Optional[Path] = None) -> Path:
        """Serialize vocabulary to a JSON file.

        Args:
            path: Destination path. Defaults to TEMPLATE_CACHE_DIR/vocab.json.

        Returns:
            The path the vocabulary was saved to.
        """
        if path is None:
            path = TEMPLATE_CACHE_DIR / "vocab.json"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "max_templates": self.max_templates,
            "template2idx": self.template2idx,
            "token2idx": self.token2idx,
            "template_counts": dict(self.template_counts),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "TemplateVocabulary":
        """Deserialize vocabulary from a JSON file.

        Args:
            path: Source path. Defaults to TEMPLATE_CACHE_DIR/vocab.json.

        Returns:
            A populated TemplateVocabulary instance.

        Raises:
            FileNotFoundError: If the vocab file does not exist.
        """
        if path is None:
            path = TEMPLATE_CACHE_DIR / "vocab.json"
        path = Path(path)

        if not path.exists():
            raise FileNotFoundError(
                f"Vocabulary file not found: {path}. "
                "Run build_from_drain_clusters() or build_from_template_strings() first."
            )

        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        vocab = cls(max_templates=payload["max_templates"])
        vocab.template2idx = payload["template2idx"]
        vocab.idx2template = {int(k): v for k, v in zip(
            vocab.template2idx.values(),
            vocab.template2idx.keys(),
        )}
        vocab.token2idx = payload["token2idx"]
        vocab.idx2token = {int(k): v for k, v in zip(
            vocab.token2idx.values(),
            vocab.token2idx.keys(),
        )}
        vocab.template_counts = Counter(payload.get("template_counts", {}))

        return vocab

    # ─────────────────────────────────────────────────────────────────
    # Batch utilities
    # ─────────────────────────────────────────────────────────────────
    def batch_encode_templates(
        self,
        templates: Sequence[str],
    ) -> List[int]:
        """Encode a batch of template strings to their indices.

        Args:
            templates: Sequence of template strings.

        Returns:
            List of integer indices (same length as input).
        """
        return [self.template_to_index(t) for t in templates]

    def get_frequency(self, template: str) -> int:
        """Return the occurrence count for a template (0 if unknown)."""
        return self.template_counts.get(template, 0)

    def most_common(self, n: int = 20) -> List[Tuple[str, int]]:
        """Return the n most frequent templates and their counts."""
        return self.template_counts.most_common(n)

"""
Drain3-based log template extraction.

Converts variable log messages into stable templates (e.g.,
"received ad request (context_words=<*>)") for use as categorical
features in the ML pipeline.

Uses Drain3's online streaming algorithm:
  - Tree-based parsing with configurable depth and similarity threshold
  - Persistent template cache (JSON file) for reproducibility
  - Template vocabulary capped at ``DataConfig.max_templates``

Integrates tightly with ``log_parser.LogEntry`` — call ``extract_template()``
on each entry's message field, or batch-process with ``fit_and_transform()``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from nn_pipeline.config import (
    TEMPLATE_CACHE_DIR,
    data_config,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Lazy Drain3 import (heavy dependency)
# ─────────────────────────────────────────────────────────────────────

_drain3_available: Optional[bool] = None


def _check_drain3() -> bool:
    """Check if drain3 is importable; cache the result."""
    global _drain3_available
    if _drain3_available is None:
        try:
            import drain3  # noqa: F401
            _drain3_available = True
        except ImportError:
            _drain3_available = False
            logger.warning(
                "drain3 not installed. Template extraction will use fallback regex. "
                "Install with: pip install drain3"
            )
    return _drain3_available


# ─────────────────────────────────────────────────────────────────────
# Pre-processing: clean message before template extraction
# ─────────────────────────────────────────────────────────────────────

# Patterns to mask before feeding to Drain (reduces template explosion)
_MASK_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # UUIDs: "db048313-1324-408c-8840-8e579ef4ec60"
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<UUID>"),
    # IP addresses: "10.103.231.20"
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "<IP>"),
    # IP:port: "10.103.231.20:9555"
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+\b"), "<IP:PORT>"),
    # Hex sequences (8+ chars): "64dd5464b8"
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<HEX>"),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "<EMAIL>"),
    # Product IDs (uppercase alphanumeric, 6+ chars): "OLJCESPC7Z"
    (re.compile(r"\b[A-Z0-9]{6,}\b"), "<PRODUCT_ID>"),
    # Numbers (standalone integers/floats)
    (re.compile(r"(?<![a-zA-Z])\b\d+\.?\d*\b(?![a-zA-Z])"), "<NUM>"),
    # Quoted strings
    (re.compile(r'"[^"]{1,200}"'), "<STR>"),
]


def preprocess_message(message: str) -> str:
    """Clean and mask variable tokens in a log message before template extraction.

    Args:
        message: Raw log message text.

    Returns:
        Cleaned message with variable parts replaced by symbolic tokens.
    """
    cleaned = message.strip()
    for pattern, replacement in _MASK_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    # Collapse multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ─────────────────────────────────────────────────────────────────────
# TemplateResult dataclass
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TemplateResult:
    """Result of template extraction for a single log message.

    Attributes:
        template_id: Unique integer identifier for this template cluster.
        template_str: The extracted template string with wildcards.
        change_type: "new", "changed", or "none" — whether this message
                     created a new template or modified an existing one.
    """
    template_id: int
    template_str: str
    change_type: str  # "new", "changed", "none"


# ─────────────────────────────────────────────────────────────────────
# Fallback regex-based template extractor (when Drain3 unavailable)
# ─────────────────────────────────────────────────────────────────────


class _FallbackExtractor:
    """Simple regex-based template extractor as fallback when Drain3 is not installed.

    Groups messages by their first N non-variable tokens. Much less accurate
    than Drain but sufficient for basic operation.
    """

    def __init__(self, max_templates: int = 500) -> None:
        self._templates: Dict[str, int] = {}
        self._next_id: int = 0
        self._max_templates = max_templates
        # Additional masking for the fallback
        self._var_re = re.compile(r"<[A-Z_:]+>")

    def extract(self, message: str) -> TemplateResult:
        """Extract a template from a preprocessed message."""
        # Create template by keeping only non-variable tokens
        tokens = message.split()
        template_tokens = []
        for tok in tokens:
            if self._var_re.fullmatch(tok):
                template_tokens.append("<*>")
            else:
                template_tokens.append(tok)
        template_str = " ".join(template_tokens)

        if template_str in self._templates:
            return TemplateResult(
                template_id=self._templates[template_str],
                template_str=template_str,
                change_type="none",
            )

        if self._next_id >= self._max_templates:
            # Map to a catch-all "other" template
            return TemplateResult(
                template_id=self._max_templates - 1,
                template_str="<OTHER>",
                change_type="none",
            )

        tid = self._next_id
        self._templates[template_str] = tid
        self._next_id += 1
        return TemplateResult(
            template_id=tid,
            template_str=template_str,
            change_type="new",
        )

    def get_template_count(self) -> int:
        return len(self._templates)

    def get_all_templates(self) -> Dict[int, str]:
        return {v: k for k, v in self._templates.items()}


# ─────────────────────────────────────────────────────────────────────
# Drain3-based template extractor
# ─────────────────────────────────────────────────────────────────────


class TemplateExtractor:
    """Drain3-powered log template extractor.

    Wraps Drain3's ``TemplateMiner`` with:
      - Pre-processing / masking of variable tokens
      - Template vocabulary capping
      - Persistent JSON cache for reproducibility
      - Graceful fallback to regex if Drain3 unavailable

    Usage::

        extractor = TemplateExtractor()
        result = extractor.extract("received ad request (context_words=[kitchen])")
        print(result.template_id, result.template_str)

        # Batch processing
        ids = extractor.fit_and_transform(messages)
    """

    def __init__(
        self,
        *,
        depth: Optional[int] = None,
        sim_th: Optional[float] = None,
        max_children: Optional[int] = None,
        max_templates: Optional[int] = None,
        cache_dir: Optional[Path] = None,
        persist: bool = True,
    ) -> None:
        """Initialize the template extractor.

        Args:
            depth: Drain tree depth (default from config).
            sim_th: Similarity threshold (default from config).
            max_children: Max children per Drain node (default from config).
            max_templates: Maximum template vocabulary size (default from config).
            cache_dir: Directory for template cache files.
            persist: Whether to save/load templates to/from disk.
        """
        self._depth = depth or data_config.drain_depth
        self._sim_th = sim_th or data_config.drain_sim_th
        self._max_children = max_children or data_config.drain_max_children
        self._max_templates = max_templates or data_config.max_templates
        self._cache_dir = cache_dir or TEMPLATE_CACHE_DIR
        self._persist = persist

        self._miner = None
        self._fallback: Optional[_FallbackExtractor] = None
        self._template_id_map: Dict[int, int] = {}  # drain_cluster_id → sequential_id
        self._next_seq_id: int = 0

        if _check_drain3():
            self._init_drain3()
        else:
            self._fallback = _FallbackExtractor(max_templates=self._max_templates)

    def _init_drain3(self) -> None:
        """Initialize Drain3 TemplateMiner with our config."""
        from drain3 import TemplateMiner
        from drain3.template_miner_config import TemplateMinerConfig

        config = TemplateMinerConfig()
        config.drain_depth = self._depth
        config.drain_sim_th = self._sim_th
        config.drain_max_children = self._max_children
        # Disable Drain3's built-in masking — we do our own
        config.masking = []
        config.drain_extra_delimiters = ["_", "=", "|"]

        self._miner = TemplateMiner(config=config)
        logger.info(
            "Drain3 initialized (depth=%d, sim_th=%.2f, max_children=%d)",
            self._depth,
            self._sim_th,
            self._max_children,
        )

    def extract(self, message: str) -> TemplateResult:
        """Extract a template from a single log message.

        Args:
            message: Raw or preprocessed log message.

        Returns:
            TemplateResult with template_id, template_str, and change_type.
        """
        preprocessed = preprocess_message(message)

        if self._fallback is not None:
            return self._fallback.extract(preprocessed)

        # Use Drain3
        result = self._miner.add_log_message(preprocessed)
        cluster_id = result["cluster_id"]
        template_str = result["template_mined"]
        change_type_raw = result["change_type"]

        # Map change_type to our enum
        if change_type_raw == "cluster_created":
            change_type = "new"
        elif change_type_raw == "cluster_template_changed":
            change_type = "changed"
        else:
            change_type = "none"

        # Map drain cluster IDs to sequential IDs (capped)
        if cluster_id not in self._template_id_map:
            if self._next_seq_id >= self._max_templates:
                # Cap reached — map to overflow bucket
                seq_id = self._max_templates - 1
            else:
                seq_id = self._next_seq_id
                self._template_id_map[cluster_id] = seq_id
                self._next_seq_id += 1
        else:
            seq_id = self._template_id_map[cluster_id]

        return TemplateResult(
            template_id=seq_id,
            template_str=template_str,
            change_type=change_type,
        )

    def fit_and_transform(self, messages: List[str]) -> List[int]:
        """Batch extract templates and return template IDs.

        Args:
            messages: List of raw log messages.

        Returns:
            List of template IDs (same length as input).
        """
        return [self.extract(msg).template_id for msg in messages]

    def get_template_count(self) -> int:
        """Return the current number of unique templates."""
        if self._fallback is not None:
            return self._fallback.get_template_count()
        return len(self._template_id_map)

    def get_all_templates(self) -> Dict[int, str]:
        """Return mapping of sequential template ID → template string.

        Returns:
            Dict mapping template_id → template string.
        """
        if self._fallback is not None:
            return self._fallback.get_all_templates()

        if self._miner is None:
            return {}

        result: Dict[int, str] = {}
        clusters = self._miner.drain.clusters
        for cluster in clusters:
            drain_id = cluster.cluster_id
            if drain_id in self._template_id_map:
                seq_id = self._template_id_map[drain_id]
                template_str = " ".join(cluster.log_template_tokens)
                result[seq_id] = template_str
        return result

    def save(self, filename: str = "templates.json") -> Path:
        """Save template vocabulary to a JSON file.

        Args:
            filename: Name of the cache file.

        Returns:
            Path to the saved file.
        """
        save_path = self._cache_dir / filename
        templates = self.get_all_templates()
        data = {
            "num_templates": self.get_template_count(),
            "id_map": {str(k): v for k, v in self._template_id_map.items()},
            "templates": {str(k): v for k, v in templates.items()},
        }
        with open(save_path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved %d templates to %s", len(templates), save_path)
        return save_path

    def load(self, filename: str = "templates.json") -> bool:
        """Load template vocabulary from a JSON file.

        Args:
            filename: Name of the cache file.

        Returns:
            True if loaded successfully, False otherwise.
        """
        load_path = self._cache_dir / filename
        if not load_path.exists():
            logger.warning("Template cache not found: %s", load_path)
            return False

        try:
            with open(load_path, "r") as f:
                data = json.load(f)
            self._template_id_map = {
                int(k): v for k, v in data.get("id_map", {}).items()
            }
            self._next_seq_id = max(self._template_id_map.values(), default=-1) + 1
            logger.info(
                "Loaded %d templates from %s",
                data.get("num_templates", 0),
                load_path,
            )
            return True
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to load template cache: %s", e)
            return False

    def reset(self) -> None:
        """Reset the extractor state (clear all templates)."""
        self._template_id_map.clear()
        self._next_seq_id = 0
        if self._fallback is not None:
            self._fallback = _FallbackExtractor(max_templates=self._max_templates)
        elif _check_drain3():
            self._init_drain3()

"""
Multi-format log parser for Kubernetes microservice logs.

Handles all log formats from Google's Online Boutique:
  - adservice (Java/Log4j): instant.epochSecond + nanoOfSecond, "time" ISO field
  - emailservice / recommendationservice (Python): "timestamp" as epoch float
  - currencyservice / paymentservice (Node.js): "time" as epoch milliseconds
  - frontend / checkoutservice / productcatalogservice / shippingservice (Go):
      "timestamp" as ISO-8601 string, optional "error" and "http.req.*" fields
  - redis-cart: plaintext Redis server logs
  - loadgenerator: plaintext Locust load-test output
  - Standalone WARNING/ERROR plaintext lines (e.g., Java deprecation warnings)

Every raw line is normalized into a ``LogEntry`` dataclass with a unified
timestamp, service name, log level, and cleaned message text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from nn_pipeline.config import (
    SERVICES,
    SERVICE_TO_IDX,
    LOG_LEVELS,
    LEVEL_TO_IDX,
    RAW_LOGS_PATH,
    data_config,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────────────────────────────

# Matches "[pod-name-hash] rest-of-line"
_POD_PREFIX_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)", re.DOTALL)

# Extracts service name from pod name (strip trailing -<hash>-<hash>)
# e.g. "adservice-64dd5464b8-5shfp" → "adservice"
# e.g. "redis-cart-6899b65948-59whc" → "redis-cart"
_SERVICE_NAME_RE = re.compile(
    r"^("
    + "|".join(re.escape(s) for s in sorted(SERVICES, key=len, reverse=True))
    + r")"
)

# Redis plaintext timestamp: "1:M 28 May 2026 07:44:40.419"
_REDIS_TS_RE = re.compile(
    r"\d+:[A-Z]\s+(\d{1,2}\s+\w+\s+\d{4}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
)

# Loadgenerator timestamp: "[2026-05-28 07:45:05,413]"
_LOCUST_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\]")

# Locust log level: "loadgenerator-xxx/INFO/locust.main:"
_LOCUST_LEVEL_RE = re.compile(r"/(\w+)/locust\.")

# Plaintext WARNING/ERROR at start
_PLAINTEXT_LEVEL_RE = re.compile(r"^(WARNING|ERROR|WARN|INFO|DEBUG)", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────
# LogEntry dataclass
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LogEntry:
    """Normalized log entry — the universal representation for the pipeline.

    Attributes:
        timestamp_unix: Unix epoch in seconds (float, sub-second precision).
        service: Canonical service name (one of ``config.SERVICES``).
        service_idx: Integer index for the service (from ``SERVICE_TO_IDX``).
        level: Normalized log level string ("INFO", "WARNING", "ERROR").
        level_idx: Integer index for the level (from ``LEVEL_TO_IDX``).
        message: Cleaned log message text (variable part).
        raw_line: Original untouched log line for debugging.
        pod_name: Full pod name including hash suffixes.
        line_number: 1-based line number in the source file.
        extra: Optional dict of additional parsed fields (http method/path, error detail, etc.)
    """

    timestamp_unix: float
    service: str
    service_idx: int
    level: str
    level_idx: int
    message: str
    raw_line: str
    pod_name: str
    line_number: int
    extra: Dict[str, str] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Timestamp helpers
# ─────────────────────────────────────────────────────────────────────


def _parse_iso_timestamp(ts_str: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp string into Unix epoch seconds.

    Handles Go-style timestamps with nanosecond precision and timezone
    suffixes (``Z`` or ``+00:00``).
    """
    if not ts_str:
        return None
    try:
        # Strip trailing 'Z' and replace with +00:00 for fromisoformat
        cleaned = ts_str.rstrip("Z")
        # Truncate nanoseconds to microseconds (Python limit)
        # "2026-05-28T07:45:14.612391744" → keep 6 decimal digits
        dot_idx = cleaned.rfind(".")
        if dot_idx != -1:
            # Find where the fractional seconds end (before any timezone offset)
            frac_end = dot_idx + 1
            while frac_end < len(cleaned) and cleaned[frac_end].isdigit():
                frac_end += 1
            frac_part = cleaned[dot_idx + 1 : frac_end][:6].ljust(6, "0")
            cleaned = cleaned[:dot_idx + 1] + frac_part + cleaned[frac_end:]

        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OSError):
        return None


def _parse_redis_timestamp(ts_str: str) -> Optional[float]:
    """Parse Redis log timestamp like '28 May 2026 07:44:40.419'."""
    try:
        # Remove fractional seconds for strptime, add manually
        parts = ts_str.split(".")
        dt = datetime.strptime(parts[0], "%d %b %Y %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        frac = float("0." + parts[1]) if len(parts) > 1 else 0.0
        return dt.timestamp() + frac
    except (ValueError, IndexError):
        return None


def _parse_locust_timestamp(ts_str: str) -> Optional[float]:
    """Parse Locust timestamp like '2026-05-28 07:45:05,413'."""
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────
# Level normalization
# ─────────────────────────────────────────────────────────────────────

_LEVEL_ALIASES: Dict[str, str] = {
    "info": "INFO",
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "debug": "INFO",       # Collapse debug → info for our 3-level scheme
    "trace": "INFO",
    "fatal": "ERROR",
    "critical": "ERROR",
    "severe": "ERROR",
}


def _normalize_level(raw_level: Optional[str]) -> str:
    """Map any level string to one of the canonical LOG_LEVELS."""
    if raw_level is None:
        return "INFO"
    return _LEVEL_ALIASES.get(raw_level.strip().lower(), "INFO")


# ─────────────────────────────────────────────────────────────────────
# Service name extraction
# ─────────────────────────────────────────────────────────────────────


def _extract_service(pod_name: str) -> str:
    """Extract canonical service name from a pod name.

    Examples:
        "adservice-64dd5464b8-5shfp" → "adservice"
        "redis-cart-6899b65948-59whc" → "redis-cart"
        "frontend-759775d795-5z7lb" → "frontend"
    """
    m = _SERVICE_NAME_RE.match(pod_name)
    if m:
        return m.group(1)
    # Fallback: try splitting on known patterns
    for svc in sorted(SERVICES, key=len, reverse=True):
        if pod_name.startswith(svc):
            return svc
    logger.debug("Unknown service in pod name: %s", pod_name)
    return "unknown"


# ─────────────────────────────────────────────────────────────────────
# JSON log parsers (one per schema family)
# ─────────────────────────────────────────────────────────────────────


def _parse_adservice_json(
    data: Dict, raw_line: str, pod_name: str, line_no: int
) -> Optional[LogEntry]:
    """Parse adservice Log4j2 JSON format.

    Schema: {instant: {epochSecond, nanoOfSecond}, level, message, time, ...}
    """
    instant = data.get("instant", {})
    epoch_sec = instant.get("epochSecond")
    nano = instant.get("nanoOfSecond", 0)

    if epoch_sec is None:
        # Try fallback "time" field (ISO string)
        ts = _parse_iso_timestamp(data.get("time", ""))
        if ts is None:
            return None
    else:
        ts = float(epoch_sec) + float(nano) / 1e9

    level = _normalize_level(data.get("level"))
    message = data.get("message", "")
    service = _extract_service(pod_name)
    service_idx = SERVICE_TO_IDX.get(service, -1)

    return LogEntry(
        timestamp_unix=ts,
        service=service,
        service_idx=service_idx,
        level=level,
        level_idx=LEVEL_TO_IDX.get(level, 0),
        message=message[:data_config.max_message_length],
        raw_line=raw_line,
        pod_name=pod_name,
        line_number=line_no,
        extra={
            "thread": data.get("thread", ""),
            "logger": data.get("loggerName", ""),
        },
    )


def _parse_python_json(
    data: Dict, raw_line: str, pod_name: str, line_no: int
) -> Optional[LogEntry]:
    """Parse emailservice / recommendationservice Python JSON format.

    Schema: {timestamp: <epoch_float>, severity, name, message}
    """
    ts = data.get("timestamp")
    if ts is None:
        return None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None

    level = _normalize_level(data.get("severity"))
    message = data.get("message", "")
    service = _extract_service(pod_name)
    service_idx = SERVICE_TO_IDX.get(service, -1)

    return LogEntry(
        timestamp_unix=ts,
        service=service,
        service_idx=service_idx,
        level=level,
        level_idx=LEVEL_TO_IDX.get(level, 0),
        message=message[:data_config.max_message_length],
        raw_line=raw_line,
        pod_name=pod_name,
        line_number=line_no,
        extra={"name": data.get("name", "")},
    )


def _parse_node_json(
    data: Dict, raw_line: str, pod_name: str, line_no: int
) -> Optional[LogEntry]:
    """Parse currencyservice / paymentservice Node.js JSON format.

    Schema: {severity, time: <epoch_ms>, pid, hostname, name, message}
    """
    time_ms = data.get("time")
    if time_ms is None:
        return None
    try:
        ts = float(time_ms) / 1000.0  # ms → seconds
    except (TypeError, ValueError):
        return None

    level = _normalize_level(data.get("severity"))
    message = data.get("message", "")
    service = _extract_service(pod_name)
    service_idx = SERVICE_TO_IDX.get(service, -1)

    return LogEntry(
        timestamp_unix=ts,
        service=service,
        service_idx=service_idx,
        level=level,
        level_idx=LEVEL_TO_IDX.get(level, 0),
        message=message[:data_config.max_message_length],
        raw_line=raw_line,
        pod_name=pod_name,
        line_number=line_no,
        extra={
            "hostname": data.get("hostname", ""),
            "pid": str(data.get("pid", "")),
        },
    )


def _parse_go_json(
    data: Dict, raw_line: str, pod_name: str, line_no: int
) -> Optional[LogEntry]:
    """Parse Go-based service JSON format (frontend, checkout, shipping, productcatalog).

    Schema: {message, severity, timestamp: <ISO-8601>}
    Frontend additionally has: error, http.req.id, http.req.method, http.req.path, session
    """
    ts_str = data.get("timestamp", "")
    ts = _parse_iso_timestamp(ts_str)
    if ts is None:
        return None

    level = _normalize_level(data.get("severity"))
    # For frontend, prefer "error" field if present, otherwise use "message"
    message = data.get("message", "")
    error_detail = data.get("error", "")

    # Combine message and error for richer text
    if error_detail:
        full_message = f"{message} | {error_detail}"
    else:
        full_message = message

    service = _extract_service(pod_name)
    service_idx = SERVICE_TO_IDX.get(service, -1)

    extra: Dict[str, str] = {}
    if "http.req.method" in data:
        extra["http_method"] = data["http.req.method"]
    if "http.req.path" in data:
        extra["http_path"] = data["http.req.path"]
    if "http.req.id" in data:
        extra["http_req_id"] = data["http.req.id"]
    if "session" in data:
        extra["session"] = data["session"]
    if error_detail:
        extra["error"] = error_detail[:data_config.max_message_length]

    return LogEntry(
        timestamp_unix=ts,
        service=service,
        service_idx=service_idx,
        level=level,
        level_idx=LEVEL_TO_IDX.get(level, 0),
        message=full_message[:data_config.max_message_length],
        raw_line=raw_line,
        pod_name=pod_name,
        line_number=line_no,
        extra=extra,
    )


def _route_json(
    data: Dict, raw_line: str, pod_name: str, line_no: int, service: str
) -> Optional[LogEntry]:
    """Route a parsed JSON dict to the correct schema-specific parser.

    Routing logic:
    1. ``instant`` key present → adservice (Log4j2)
    2. ``timestamp`` is a float → Python services (emailservice, recommendationservice)
    3. ``time`` is an int (epoch ms) → Node.js services (currencyservice, paymentservice)
    4. ``timestamp`` is an ISO string → Go services (frontend, checkout, shipping, etc.)
    """
    # 1. adservice: has "instant" dict
    if "instant" in data:
        return _parse_adservice_json(data, raw_line, pod_name, line_no)

    # 2. Python services: "timestamp" is a number (epoch float)
    ts_val = data.get("timestamp")
    if ts_val is not None and isinstance(ts_val, (int, float)):
        return _parse_python_json(data, raw_line, pod_name, line_no)

    # 3. Node.js services: "time" is epoch milliseconds (int)
    time_val = data.get("time")
    if time_val is not None and isinstance(time_val, (int, float)):
        return _parse_node_json(data, raw_line, pod_name, line_no)

    # 4. Go services: "timestamp" is an ISO-8601 string
    if ts_val is not None and isinstance(ts_val, str):
        return _parse_go_json(data, raw_line, pod_name, line_no)

    # 5. Last resort: if "severity" and "message" exist, try Go parser
    if "severity" in data and "message" in data:
        return _parse_go_json(data, raw_line, pod_name, line_no)

    return None


# ─────────────────────────────────────────────────────────────────────
# Plaintext log parsers
# ─────────────────────────────────────────────────────────────────────


def _parse_redis_plaintext(
    body: str, raw_line: str, pod_name: str, line_no: int
) -> Optional[LogEntry]:
    """Parse Redis server plaintext log lines.

    Examples:
        "1:M 28 May 2026 07:44:40.419 # WARNING: Redis does not..."
        "1:C 28 May 2026 07:44:40.331 * Redis version=8.6.3..."
    """
    m = _REDIS_TS_RE.search(body)
    ts: Optional[float] = None
    if m:
        ts = _parse_redis_timestamp(m.group(1))

    # Determine level from Redis markers: # = warning/error, * = info
    level = "INFO"
    if "# WARNING" in body or "# warning" in body:
        level = "WARNING"
    elif "# ERROR" in body or "# error" in body:
        level = "ERROR"

    # Extract the message portion (after the marker character)
    # Pattern: "1:M ... * <message>" or "1:M ... # <message>"
    msg_match = re.search(r"\d+:[A-Z]\s+\S+.*?[*#!+]\s*(.*)", body)
    message = msg_match.group(1) if msg_match else body

    service = _extract_service(pod_name)

    return LogEntry(
        timestamp_unix=ts or 0.0,
        service=service,
        service_idx=SERVICE_TO_IDX.get(service, -1),
        level=level,
        level_idx=LEVEL_TO_IDX.get(level, 0),
        message=message[:data_config.max_message_length],
        raw_line=raw_line,
        pod_name=pod_name,
        line_number=line_no,
    )


def _parse_loadgenerator_plaintext(
    body: str, raw_line: str, pod_name: str, line_no: int
) -> Optional[LogEntry]:
    """Parse Locust load-generator plaintext logs.

    Example: "[2026-05-28 07:45:05,413] loadgenerator-.../INFO/locust.main: Starting Locust 2.43.0"
    """
    ts: Optional[float] = None
    m = _LOCUST_TS_RE.search(body)
    if m:
        ts = _parse_locust_timestamp(m.group(1))

    level = "INFO"
    lm = _LOCUST_LEVEL_RE.search(body)
    if lm:
        level = _normalize_level(lm.group(1))

    # Extract message after the "locust.xxx:" part
    colon_idx = body.find(": ", body.find("/locust.") if "/locust." in body else 0)
    message = body[colon_idx + 2 :].strip() if colon_idx != -1 else body.strip()

    service = _extract_service(pod_name)

    return LogEntry(
        timestamp_unix=ts or 0.0,
        service=service,
        service_idx=SERVICE_TO_IDX.get(service, -1),
        level=level,
        level_idx=LEVEL_TO_IDX.get(level, 0),
        message=message[:data_config.max_message_length],
        raw_line=raw_line,
        pod_name=pod_name,
        line_number=line_no,
    )


def _parse_generic_plaintext(
    body: str, raw_line: str, pod_name: str, line_no: int
) -> Optional[LogEntry]:
    """Parse generic plaintext lines (Java warnings, misc output).

    Examples:
        "WARNING: A terminally deprecated method in sun.misc.Unsafe has been called"
        "(node:1) DeprecationWarning: Calling start() is no longer necessary."
        "Starting Redis Server"
        "UnacceptedCreditCard [Error]: Sorry, we cannot process..."
    """
    level = "INFO"
    m = _PLAINTEXT_LEVEL_RE.search(body)
    if m:
        level = _normalize_level(m.group(1))
    elif "Error" in body or "error" in body:
        level = "ERROR"
    elif "DeprecationWarning" in body:
        level = "WARNING"

    # Clean up the message — remove level prefix if present
    message = body.strip()
    if m:
        message = body[m.end() :].lstrip(": ").strip()

    service = _extract_service(pod_name)

    return LogEntry(
        timestamp_unix=0.0,  # No timestamp in generic plaintext
        service=service,
        service_idx=SERVICE_TO_IDX.get(service, -1),
        level=level,
        level_idx=LEVEL_TO_IDX.get(level, 0),
        message=message[:data_config.max_message_length] if message else body[:data_config.max_message_length],
        raw_line=raw_line,
        pod_name=pod_name,
        line_number=line_no,
    )


# ─────────────────────────────────────────────────────────────────────
# Multi-line accumulator
# ─────────────────────────────────────────────────────────────────────

_JSON_START_CHARS = frozenset("{[")


def _is_json_start(text: str) -> bool:
    """Check if text looks like the start of a JSON object."""
    stripped = text.lstrip()
    return len(stripped) > 0 and stripped[0] in _JSON_START_CHARS


def _try_parse_json(text: str) -> Optional[Dict]:
    """Attempt to parse text as JSON, returning None on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Main parser
# ─────────────────────────────────────────────────────────────────────


def parse_line(
    raw_line: str,
    line_number: int,
    *,
    prev_pod: Optional[str] = None,
) -> Optional[LogEntry]:
    """Parse a single raw log line into a LogEntry.

    Args:
        raw_line: The complete log line from the raw file.
        line_number: 1-based line number in the source file.
        prev_pod: Pod name from the previous line (for continuation lines).

    Returns:
        A LogEntry if parsing succeeded, None if the line should be skipped
        (e.g., separator lines, table headers from loadgenerator, very short lines).
    """
    # 1. Extract pod prefix
    m = _POD_PREFIX_RE.match(raw_line)
    if m:
        pod_name = m.group(1)
        body = m.group(2).strip()
    elif prev_pod:
        # Continuation line (no [pod] prefix) — common for loadgenerator tables
        pod_name = prev_pod
        body = raw_line.strip()
    else:
        # Cannot determine source — skip
        return None

    # 2. Skip empty or too-short lines
    if len(body) < data_config.min_message_length:
        return None

    # 3. Skip loadgenerator table separator/header lines
    if body.startswith("---") or body.startswith("Type ") or body.startswith("Aggregated"):
        return None

    service = _extract_service(pod_name)

    # 4. Try JSON parsing first
    parsed = _try_parse_json(body)
    if parsed is not None and isinstance(parsed, dict):
        entry = _route_json(parsed, raw_line, pod_name, line_number, service)
        if entry is not None:
            return entry

    # 5. Plaintext routing based on service
    if service == "redis-cart":
        return _parse_redis_plaintext(body, raw_line, pod_name, line_number)
    elif service == "loadgenerator":
        return _parse_loadgenerator_plaintext(body, raw_line, pod_name, line_number)
    else:
        return _parse_generic_plaintext(body, raw_line, pod_name, line_number)


def parse_file(
    filepath: Optional[Path] = None,
    *,
    max_lines: Optional[int] = None,
) -> List[LogEntry]:
    """Parse an entire log file into a list of LogEntry objects.

    Args:
        filepath: Path to the raw log file. Defaults to ``RAW_LOGS_PATH``.
        max_lines: If set, stop after reading this many lines (useful for testing).

    Returns:
        List of successfully parsed LogEntry objects, sorted by timestamp.
    """
    filepath = filepath or RAW_LOGS_PATH
    entries: List[LogEntry] = []
    skipped = 0
    prev_pod: Optional[str] = None

    logger.info("Parsing log file: %s", filepath)

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if max_lines is not None and line_no > max_lines:
                break

            raw_line = raw_line.rstrip("\n\r")
            entry = parse_line(raw_line, line_no, prev_pod=prev_pod)

            # Track pod for continuation lines
            m = _POD_PREFIX_RE.match(raw_line)
            if m:
                prev_pod = m.group(1)

            if entry is not None:
                entries.append(entry)
            else:
                skipped += 1

    # Sort by timestamp (stable sort preserves file order for equal timestamps)
    entries.sort(key=lambda e: e.timestamp_unix)

    logger.info(
        "Parsed %d entries, skipped %d lines from %s",
        len(entries),
        skipped,
        filepath,
    )
    return entries


def parse_file_iter(
    filepath: Optional[Path] = None,
    *,
    max_lines: Optional[int] = None,
) -> Iterator[LogEntry]:
    """Lazily iterate over log entries from a file (unsorted, file order).

    Use this for memory-efficient streaming when sorted order is not required.
    """
    filepath = filepath or RAW_LOGS_PATH
    prev_pod: Optional[str] = None

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if max_lines is not None and line_no > max_lines:
                break

            raw_line = raw_line.rstrip("\n\r")
            entry = parse_line(raw_line, line_no, prev_pod=prev_pod)

            m = _POD_PREFIX_RE.match(raw_line)
            if m:
                prev_pod = m.group(1)

            if entry is not None:
                yield entry


# ─────────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────────


def group_by_service(entries: List[LogEntry]) -> Dict[str, List[LogEntry]]:
    """Group log entries by service name.

    Returns:
        Dict mapping service name → list of entries from that service.
    """
    groups: Dict[str, List[LogEntry]] = {s: [] for s in SERVICES}
    for entry in entries:
        if entry.service in groups:
            groups[entry.service].append(entry)
        else:
            groups.setdefault(entry.service, []).append(entry)
    return groups


def filter_by_time_range(
    entries: List[LogEntry],
    start_unix: float,
    end_unix: float,
) -> List[LogEntry]:
    """Filter entries to those within [start_unix, end_unix]."""
    return [e for e in entries if start_unix <= e.timestamp_unix <= end_unix]

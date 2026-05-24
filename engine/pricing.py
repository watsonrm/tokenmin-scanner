"""Pricing lookup. Reads pricing.json from this directory.

SPDX-License-Identifier: Apache-2.0

Why this lives next to the engine (and not baked into source):
    Anthropic updates pricing periodically. Baking dollar amounts into Python
    means every installed tokenmin quietly misreports until users update the
    client. Loading from `pricing.json` next to the engine means a single
    edit + push refreshes every install via the existing auto-update path
    (`git pull` on the bundle repo).

Stale-pricing safety:
    The JSON carries a `last_updated` date + a `stale_warning_days` threshold.
    `is_stale()` returns True when the data is older than the threshold so
    the CLI can warn the user (and themselves) before reporting dollar
    numbers that may no longer match Anthropic's published rates.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"

# Default if pricing.json is missing or corrupt. Kept current as of the
# release date so the tool degrades to "slightly stale" rather than $0.
_DEFAULT = {
    "schema_version": 1,
    "last_updated": "2026-05-23",
    "models": {
        "opus":   {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
        "sonnet": {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
        "haiku":  {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read": 0.08},
    },
    "fallback": "sonnet",
    "stale_warning_days": 60,
}


_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(_PRICING_PATH.read_text(encoding="utf-8"))
        # Trust the file but require the model table to be present and a dict.
        if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
            data = _DEFAULT
    except (OSError, json.JSONDecodeError):
        data = _DEFAULT
    _cache = data
    return _cache


def price_for(model: str | None) -> tuple[float, float, float, float]:
    """Return (input, output, cache_write, cache_read) USD per million tokens.

    Lookup is substring-based against the pricing.json `models` keys
    (lowercased). Unknown model → `fallback` (defaults to "sonnet").
    """
    data = _load()
    models = data.get("models", {})
    fallback_key = data.get("fallback", "sonnet")
    m = None
    if model:
        ml = model.lower()
        for key, prices in models.items():
            if key in ml:
                m = prices
                break
    if m is None:
        m = models.get(fallback_key, _DEFAULT["models"]["sonnet"])
    return (
        float(m.get("input", 0.0)),
        float(m.get("output", 0.0)),
        float(m.get("cache_write", 0.0)),
        float(m.get("cache_read", 0.0)),
    )


def pricing_metadata() -> dict:
    """Loaded-from-file metadata: last_updated, source, schema_version, etc."""
    return dict(_load())


def pricing_age_days() -> int | None:
    """How many days old the pricing data is. None if unparseable."""
    data = _load()
    last = str(data.get("last_updated", ""))
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def is_stale() -> bool:
    """True if pricing data is older than its own stale_warning_days threshold."""
    data = _load()
    threshold = int(data.get("stale_warning_days", 60))
    age = pricing_age_days()
    return age is not None and age > threshold

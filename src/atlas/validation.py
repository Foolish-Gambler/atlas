"""Shared input guards for symbols, quantities, prices, and probabilities."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


class InvalidInputError(ValueError):
    """Raised when a numeric or symbol input cannot be used safely."""


def normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise InvalidInputError("symbol must be a string.")
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise InvalidInputError("symbol is required.")
    if any(char.isspace() for char in cleaned):
        raise InvalidInputError("symbol must not contain internal whitespace.")
    if not all(char.isalnum() or char in "-." for char in cleaned):
        raise InvalidInputError(f"symbol {cleaned!r} contains invalid characters.")
    return cleaned


def require_finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInputError(f"{name} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidInputError(f"{name} must be a finite number.")
    return number


def require_positive_finite(value: Any, name: str) -> float:
    number = require_finite(value, name)
    if number <= 0:
        raise InvalidInputError(f"{name} must be positive.")
    return number


def require_non_negative_finite(value: Any, name: str) -> float:
    number = require_finite(value, name)
    if number < 0:
        raise InvalidInputError(f"{name} must be non-negative.")
    return number


def require_unit_interval(value: Any, name: str) -> float:
    number = require_finite(value, name)
    if number < 0.0 or number > 1.0:
        raise InvalidInputError(f"{name} must be between 0 and 1 inclusive.")
    return number


def require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInputError(f"{name} must be an integer.")
    return value


def require_aware_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidInputError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise InvalidInputError(f"{name} must be timezone-aware.")
    return value

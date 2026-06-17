"""Internal DynamoDB marshaling helpers.

We use the low-level boto3 client (one client, built in ``clients.py``) but want
to work in plain Python dicts. These helpers convert between dicts and DynamoDB
attribute-value maps, sanitizing floats to ``Decimal`` (DynamoDB rejects floats)
and dropping ``None`` values (no attribute is written for them).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

_ser = TypeSerializer()
_deser = TypeDeserializer()


def _sanitize(value: Any) -> Any:
    """Recursively make a value DynamoDB-safe (float -> Decimal)."""
    if isinstance(value, float):
        # str() first to avoid binary float artifacts (e.g. 0.1 -> 0.1000...).
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def to_value(value: Any) -> Any:
    """Serialize one Python value into a DynamoDB attribute-value."""
    return _ser.serialize(_sanitize(value))


def to_item(data: dict[str, Any]) -> dict[str, Any]:
    """Serialize a plain dict into a DynamoDB attribute-value item."""
    return {key: to_value(val) for key, val in data.items() if val is not None}


def _unwrap(value: Any) -> Any:
    """Convert Decimals back to int/float when reading items out."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


def from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Deserialize a DynamoDB attribute-value item into a plain dict."""
    return {key: _unwrap(_deser.deserialize(val)) for key, val in item.items()}

"""Input validation helpers shared across ragkit providers.

These are the domain-agnostic copies of the validators that the
application's pgvector store defines. A later plan rewires the
application to import these copies; the originals are removed once
every consumer has migrated.
"""

from __future__ import annotations

import math
import re

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_identifier(name: str, label: str) -> None:
    """Validate that a string is a safe SQL identifier (table, column, etc.).

    Only allows alphanumeric characters and underscores, starting with a
    letter or underscore. This is a strict whitelist that prevents SQL
    injection through identifier interpolation.

    Args:
        name: The identifier string to validate.
        label: Human-readable label for error messages.

    Raises:
        ValueError: If the identifier contains unsafe characters.
    """
    if not _IDENTIFIER_PATTERN.fullmatch(name):
        raise ValueError(f"Unsafe {label}: {name!r}")


def validate_embedding(
    embedding: list[float],
    expected_dim: int,
    label: str = "embedding",
) -> None:
    """Validate that an embedding vector has the expected dimension and contains only finite values.

    Args:
        embedding: The embedding vector to validate.
        expected_dim: Expected dimensionality of the embedding.
        label: Human-readable label for error messages.

    Raises:
        ValueError: If the embedding has the wrong dimension or contains non-finite values.
        TypeError: If a value in the embedding is not a number.
    """
    if len(embedding) != expected_dim:
        raise ValueError(f"{label} has {len(embedding)} dimensions; expected {expected_dim}")
    for i, value in enumerate(embedding):
        try:
            if not math.isfinite(value):
                raise ValueError(f"{label}[{i}] is not finite: {value!r}")
        except TypeError as exc:
            raise TypeError(f"{label}[{i}] is not a number: {value!r}") from exc

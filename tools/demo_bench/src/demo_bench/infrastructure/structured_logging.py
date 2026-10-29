# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structured (JSON lines) logging to stderr; stdout stays reserved for command output."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# Attributes every LogRecord has; anything else came from ``extra=`` and is emitted as a field.
_RESERVED = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a record.

        Args:
            record: Log record.

        Returns:
            One JSON line.
        """
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                doc[key] = value
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Install the JSON formatter on the ``demo_bench`` logger.

    Records still propagate to the root logger: the CLI installs no root handler, so nothing
    is printed twice, and an embedding process (or pytest's log capture) still receives them.

    Args:
        level: Log level name.
        stream: Destination (default ``sys.stderr``).
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("demo_bench")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

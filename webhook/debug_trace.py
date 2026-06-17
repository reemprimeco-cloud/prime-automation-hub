"""Debug-mode NDJSON trace (session dc5b55)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from logging_config import get_logger

_SESSION = "dc5b55"
_LOG_PATH = Path(__file__).resolve().parent.parent / ".cursor" / "debug-dc5b55.log"
_LOG = get_logger("webhook.debug")


def debug_trace(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    payload = data or {}
    entry = {
        "sessionId": _SESSION,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": payload,
        "timestamp": int(time.time() * 1000),
    }
    _LOG.info(
        "debug_trace",
        extra={
            "debug_session": _SESSION,
            "hypothesis_id": hypothesis_id,
            "debug_location": location,
            "debug_message": message,
            **{f"debug_{k}": v for k, v in payload.items()},
        },
    )
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    # #endregion

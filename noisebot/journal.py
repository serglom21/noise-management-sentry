"""One JSON object per decision. Never includes event bodies or secrets."""

from __future__ import annotations

import json
import sys
from typing import Any


def journal(
    *,
    issue_id: str | int,
    title: str | None,
    count: int | None,
    decision: str,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    record = {
        "issue_id": str(issue_id),
        "title": title or "",
        "count": int(count or 0),
        "decision": decision,
        "reasons": list(reasons or []),
    }
    sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return record

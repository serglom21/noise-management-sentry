"""Refuse to discard anything that might be a new or current-release issue."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from noisebot.config import Settings


def _parse_time(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def issue_age_days(first_seen: str, *, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current - _parse_time(first_seen)).total_seconds() / 86400


def guardrail_failures(
    issue: dict[str, Any],
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Reasons this issue must not be discarded. An empty list means it may proceed.

    ``issue`` is the normalized dict from ``noisebot.events.normalize_issue``:
    ``project``, ``first_seen``, ``release``, ``latest_release``, ``level``,
    ``assigned_to``. Missing proof fails closed: an unknown release is not discarded.
    """

    if settings is None:
        from noisebot.config import load_settings

        settings = load_settings()

    reasons: list[str] = []
    project = issue.get("project")
    if not project or project not in settings.allowed_projects:
        reasons.append("project_not_allowed")

    first_seen = issue.get("first_seen")
    if not first_seen:
        reasons.append("too_young")
    else:
        try:
            age = issue_age_days(str(first_seen), now=now)
        except ValueError:
            reasons.append("too_young")
        else:
            if age < settings.min_age_days:
                reasons.append("too_young")

    release = issue.get("release")
    latest = issue.get("latest_release")
    if not release:
        reasons.append("release_unknown")
    elif not latest:
        reasons.append("latest_release_unknown")
    elif release == latest:
        reasons.append("latest_release")

    if str(issue.get("level") or "").lower() == "fatal":
        reasons.append("fatal")

    if settings.require_owner and not issue.get("assigned_to"):
        reasons.append("missing_owner")

    return reasons

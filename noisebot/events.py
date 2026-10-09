"""Pull release, exception type, and the crashing in-app frame off an event.

Works for both the issue-event API (``entries``) and an ``event_alert`` webhook
(``exception`` plus tags as ``[key, value]`` pairs).
"""

from __future__ import annotations

from typing import Any


def tag_value(event: dict | None, key: str) -> str | None:
    if not event:
        return None
    tags = event.get("tags")
    if isinstance(tags, dict):
        value = tags.get(key)
        return None if value is None else str(value)
    if not isinstance(tags, list):
        return None
    for item in tags:
        if isinstance(item, dict) and item.get("key") == key and item.get("value") is not None:
            return str(item["value"])
        if isinstance(item, (list, tuple)) and len(item) >= 2 and item[0] == key and item[1] is not None:
            return str(item[1])
    return None


def event_release(event: dict | None) -> str | None:
    if not event:
        return None
    release = event.get("release")
    if isinstance(release, str) and release:
        return release
    if isinstance(release, dict):
        version = release.get("version")
        if version:
            return str(version)
    return tag_value(event, "release")


def event_level(event: dict | None, fallback: str | None = None) -> str:
    if event:
        level = event.get("level")
        if isinstance(level, str) and level:
            return level.lower()
        tagged = tag_value(event, "level")
        if tagged:
            return tagged.lower()
    return (fallback or "").lower()


def _exception_values(event: dict) -> list[dict]:
    exception = event.get("exception")
    if isinstance(exception, dict) and isinstance(exception.get("values"), list):
        return [value for value in exception["values"] if isinstance(value, dict)]
    for entry in event.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("type") != "exception":
            continue
        values = (entry.get("data") or {}).get("values") or []
        return [value for value in values if isinstance(value, dict)]
    return []


def exception_type(event: dict | None) -> str | None:
    if not event:
        return None
    values = _exception_values(event)
    if not values:
        return None
    kind = values[-1].get("type")
    return str(kind) if kind else None


def crashing_in_app_function(event: dict | None) -> str | None:
    """Function name of the last in-app frame.

    Frames are oldest-first, so the last ``in_app`` frame is the crashing one.
    ``app:yes`` in a fingerprint rule matches that same frame's function.
    """

    if not event:
        return None
    values = _exception_values(event)
    if not values:
        return None
    frames = ((values[-1].get("stacktrace") or {}).get("frames")) or []
    for frame in reversed(frames):
        if not isinstance(frame, dict):
            continue
        in_app = frame.get("in_app")
        if in_app is None:
            in_app = frame.get("inApp")
        if in_app and frame.get("function"):
            return str(frame["function"])
    return None


def project_slug(issue: dict) -> str | None:
    project = issue.get("project")
    if isinstance(project, dict) and project.get("slug"):
        return str(project["slug"])
    if isinstance(project, str) and project and not project.isdigit():
        return project
    return None


def normalize_issue(
    issue: dict,
    *,
    event: dict | None,
    latest_release: str | None,
    window_count: int,
) -> dict[str, Any]:
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    error_type = exception_type(event) or metadata.get("type")
    function = crashing_in_app_function(event) or metadata.get("function")
    return {
        "id": str(issue.get("id")),
        "title": issue.get("title") or "",
        "count": int(window_count),
        "level": event_level(event, issue.get("level")),
        "first_seen": issue.get("firstSeen") or issue.get("first_seen"),
        "project": project_slug(issue),
        "assigned_to": issue.get("assignedTo", issue.get("assigned_to")),
        "release": event_release(event),
        "latest_release": latest_release,
        "error_type": str(error_type) if error_type else None,
        "function": str(function) if function else None,
    }

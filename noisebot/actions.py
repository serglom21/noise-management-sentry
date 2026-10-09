"""Discard, merge, and fingerprint. Every mutating call is allowlisted and dry-run aware."""

from __future__ import annotations

from noisebot.api import ProjectNotAllowed, SentryClient
from noisebot.config import Settings
from noisebot.events import crashing_in_app_function, exception_type

# Glob metacharacters, plus the characters the quoted-string parser treats specially.
_GLOB_CHARS = set("*?[]{}")


def escape_glob(value: str) -> str:
    """Escape a matcher value for a quoted fingerprint rule.

    The fingerprint parser unescapes quotes first (``\\\\`` becomes ``\\``, and
    an invalid escape like ``\\*`` is kept as a backslash plus star). The glob
    matcher then treats ``\\*`` as a literal asterisk. Docs:
    https://docs.sentry.io/concepts/data-management/event-grouping/fingerprint-rules/
    """

    pieces: list[str] = []
    for char in value:
        if char == "\\":
            pieces.append("\\\\")
        elif char == '"':
            pieces.append('\\"')
        elif char in _GLOB_CHARS:
            pieces.append("\\" + char)
        else:
            pieces.append(char)
    return "".join(pieces)


def format_fingerprint_rule(error_type: str, function: str, issue_id: str) -> str:
    """Match exception type plus the crashing in-app frame's function.

    ``app:yes`` and ``stack.function`` have to hit the same frame. Using the
    last in-app frame's name is as specific as the rule language gets.
    """

    safe_id = str(issue_id)
    return (
        f'error.type:"{escape_glob(error_type)}" '
        f'app:yes stack.function:"{escape_glob(function)}" '
        f"-> noise-{safe_id}"
    )


def build_fingerprint_rule(issue_id: str, *, client: SentryClient, event: dict | None = None) -> str:
    if event is None:
        event = client.latest_event(str(issue_id))
    error_type = exception_type(event)
    function = crashing_in_app_function(event)
    if not error_type or not function:
        raise ValueError(
            f"Issue {issue_id} has no exception type or in-app crashing frame to fingerprint."
        )
    return format_fingerprint_rule(error_type, function, str(issue_id))


def rule_present(existing: str | None, rule: str) -> bool:
    wanted = rule.strip()
    if not wanted:
        return False
    return any(line.strip() == wanted for line in (existing or "").splitlines())


def join_rules(existing: str | None, rule: str) -> str:
    if rule_present(existing, rule):
        return existing or ""
    base = existing or ""
    if base.strip() == "":
        return rule.strip() + "\n"
    if not base.endswith("\n"):
        base += "\n"
    return base + rule.strip() + "\n"


def append_fingerprint_rule(
    project: str,
    rule: str,
    *,
    client: SentryClient,
    settings: Settings,
) -> str:
    """Add ``rule`` if it is not already in the project config. Returns the decision."""

    if project not in settings.allowed_projects:
        raise ProjectNotAllowed(project)
    current = client.get_project(project)
    existing = current.get("fingerprintingRules") or ""
    if rule_present(existing, rule):
        return "fingerprint_present"
    if settings.dry_run:
        return "would_fingerprint"
    client.update_project(project, {"fingerprintingRules": join_rules(existing, rule)})
    return "fingerprint"


def discard(issue: dict, *, client: SentryClient, settings: Settings) -> str:
    project = issue.get("project") or ""
    if project not in settings.allowed_projects:
        raise ProjectNotAllowed(project)
    if settings.dry_run:
        return "would_discard"
    client.discard_issue(project, str(issue["id"]))
    return "discard"


def merge(issue_ids: list[str], project: str, *, client: SentryClient, settings: Settings) -> tuple[str, str]:
    """Returns ``(decision, parent_id)``. In dry-run the first id stands in as parent."""

    if project not in settings.allowed_projects:
        raise ProjectNotAllowed(project)
    if len(issue_ids) < 2:
        return "merge_skipped", issue_ids[0]
    if settings.dry_run:
        return "would_merge", str(issue_ids[0])
    parent = client.merge_issues(project, [str(issue_id) for issue_id in issue_ids])
    return "merge", parent

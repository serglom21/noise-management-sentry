"""Guardrails, ledger, then discard. Shared by the pull job and the webhook."""

from __future__ import annotations

from noisebot.actions import discard, merge
from noisebot.api import SentryApiError, SentryClient
from noisebot.config import Settings
from noisebot.events import normalize_issue
from noisebot.guardrails import guardrail_failures
from noisebot.journal import journal
from noisebot.ledger import SqliteLedger


def consider(
    issue: dict,
    *,
    settings: Settings,
    ledger: SqliteLedger,
    client: SentryClient,
    because: str = "threshold_met",
) -> str:
    """Apply guardrails and the ledger, then discard. Returns the decision."""

    reasons = guardrail_failures(issue, settings)
    if reasons:
        journal(
            issue_id=issue.get("id", ""),
            title=issue.get("title"),
            count=issue.get("count"),
            decision="skip",
            reasons=reasons,
        )
        return "skip"

    issue_id = str(issue["id"])
    reservation = ledger.try_begin(issue_id)
    if reservation != "ok":
        reason = "already_actioned" if reservation == "duplicate" else "daily_cap"
        journal(
            issue_id=issue_id,
            title=issue.get("title"),
            count=issue.get("count"),
            decision="skip",
            reasons=[reason],
        )
        return "skip"

    try:
        decision = discard(issue, client=client, settings=settings)
    except Exception as exc:
        ledger.release(issue_id)
        detail = exc.detail if isinstance(exc, SentryApiError) else exc.__class__.__name__
        journal(
            issue_id=issue_id,
            title=issue.get("title"),
            count=issue.get("count"),
            decision="error",
            reasons=[str(detail)[:300]],
        )
        return "error"

    journal(
        issue_id=issue_id,
        title=issue.get("title"),
        count=issue.get("count"),
        decision=decision,
        reasons=[because] if decision in {"discard", "would_discard"} else [],
    )
    return decision


def _discard_reserved(
    issue: dict,
    *,
    settings: Settings,
    ledger: SqliteLedger,
    client: SentryClient,
    extra_reasons: list[str] | None = None,
) -> str:
    """Discard an issue whose ledger row was already reserved."""

    try:
        decision = discard(issue, client=client, settings=settings)
    except Exception as exc:
        ledger.release(str(issue["id"]))
        detail = exc.detail if isinstance(exc, SentryApiError) else exc.__class__.__name__
        journal(
            issue_id=issue.get("id", ""),
            title=issue.get("title"),
            count=issue.get("count"),
            decision="error",
            reasons=[str(detail)[:300]],
        )
        return "error"
    journal(
        issue_id=issue.get("id", ""),
        title=issue.get("title"),
        count=issue.get("count"),
        decision=decision,
        reasons=["threshold_met", *(extra_reasons or [])],
    )
    return decision


def consider_cluster(
    issues: list[dict],
    *,
    settings: Settings,
    ledger: SqliteLedger,
    client: SentryClient,
) -> str:
    """Merge issues that share a type and crashing function, then discard the parent.

    Each issue must already have passed the guardrails. The ledger reserves every
    id so none of them can be discarded again. The cap counts each issue.
    """

    if not issues:
        return "skip"
    if len(issues) == 1:
        return consider(issues[0], settings=settings, ledger=ledger, client=client)

    project = issues[0].get("project") or ""
    reserved: list[dict] = []
    for issue in issues:
        reservation = ledger.try_begin(str(issue["id"]))
        if reservation != "ok":
            reason = "already_actioned" if reservation == "duplicate" else "daily_cap"
            journal(
                issue_id=issue["id"],
                title=issue.get("title"),
                count=issue.get("count"),
                decision="skip",
                reasons=[reason, "cluster"],
            )
            continue
        reserved.append(issue)

    if not reserved:
        return "skip"
    if len(reserved) == 1:
        return _discard_reserved(reserved[0], settings=settings, ledger=ledger, client=client)

    ids = [str(issue["id"]) for issue in reserved]
    try:
        merge_decision, parent = merge(ids, project, client=client, settings=settings)
    except Exception as exc:
        for issue in reserved:
            ledger.release(str(issue["id"]))
        detail = exc.detail if isinstance(exc, SentryApiError) else exc.__class__.__name__
        journal(
            issue_id=ids[0],
            title=reserved[0].get("title"),
            count=reserved[0].get("count"),
            decision="error",
            reasons=[str(detail)[:300]],
        )
        return "error"

    journal(
        issue_id=parent,
        title=reserved[0].get("title"),
        count=sum(int(issue.get("count") or 0) for issue in reserved),
        decision=merge_decision,
        reasons=["consolidate", *[f"child:{issue_id}" for issue_id in ids if issue_id != str(parent)]],
    )
    parent_issue = next((issue for issue in reserved if str(issue["id"]) == str(parent)), reserved[0])
    parent_issue = {**parent_issue, "id": str(parent), "project": project}
    return _discard_reserved(
        parent_issue,
        settings=settings,
        ledger=ledger,
        client=client,
        extra_reasons=["consolidated"],
    )


def load_normalized(
    issue: dict,
    *,
    client: SentryClient,
    latest_release: str | None,
    window_count: int,
    event: dict | None = None,
) -> dict:
    if event is None:
        event = client.latest_event(str(issue["id"]))
    return normalize_issue(
        issue,
        event=event,
        latest_release=latest_release,
        window_count=window_count,
    )

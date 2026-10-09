#!/usr/bin/env python3
"""Check a live sandbox run: noise discarded, new and signal issues left alone.

Expects ``state/issues.json`` from ``generate.py``, and a pull that already ran
with ``DRY_RUN=false`` and ``MIN_AGE_DAYS=0``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from noisebot.api import SentryClient
from noisebot.config import DEFAULT_STATE, ROOT, describe, load_settings


def _load_state() -> dict:
    if not DEFAULT_STATE.is_file():
        raise SystemExit(f"Missing {DEFAULT_STATE}. Run generate.py first.")
    return json.loads(DEFAULT_STATE.read_text())


def _ids(state: dict, key: str) -> list[str]:
    return [str(row["id"]) for row in state.get(key) or [] if row.get("id")]


def _unresolved_ids(client: SentryClient, project: str) -> set[str]:
    issues = client.list_issues(project, query="is:unresolved")
    return {str(issue.get("id")) for issue in issues}


def _filtered_quantity(groups: list[dict]) -> tuple[int, list[str]]:
    total = 0
    reasons: list[str] = []
    for group in groups:
        by = group.get("by") or {}
        outcome = str(by.get("outcome") or "")
        reason = str(by.get("reason") or "")
        totals = group.get("totals") or {}
        quantity = totals.get("sum(quantity)") or 0
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            quantity = 0
        if outcome == "filtered":
            total += quantity
            if reason:
                reasons.append(f"{reason}={quantity}")
    return total, reasons


def _wait_until(predicate, timeout: int, interval: int = 5):
    deadline = time.time() + timeout
    last = predicate()
    while time.time() < deadline:
        if last[0]:
            return last
        time.sleep(interval)
        last = predicate()
    return last


def _row(name: str, ok: bool, detail: str) -> tuple[str, str, str]:
    return name, "pass" if ok else "fail", detail


def main(argv: list[str] | None = None) -> int:
    settings = load_settings(
        dotenv=True,
        require=("SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_TOKEN", "SENTRY_DSN"),
    )
    print(describe(settings), file=sys.stderr)
    state = _load_state()
    noise_ids = _ids(state, "noise")
    new_ids = _ids(state, "new")
    signal_ids = _ids(state, "signal")
    if not noise_ids or not new_ids or not signal_ids:
        raise SystemExit("state/issues.json needs noise, new, and signal issue ids. Run generate.py --scenario all.")

    client = SentryClient(settings)
    project = client.get_project(settings.project)
    project_id = str(project["id"])
    rows: list[tuple[str, str, str]] = []

    unresolved = _unresolved_ids(client, settings.project)
    noise_gone = all(issue_id not in unresolved for issue_id in noise_ids)
    rows.append(_row("noise absent from unresolved search", noise_gone, ", ".join(noise_ids)))

    tombstones = client.list_tombstones(settings.project)
    types = {
        str((item.get("metadata") or {}).get("type") or "")
        for item in tombstones
    }
    messages = [str(item.get("message") or "") for item in tombstones]
    tombstone_hit = "LegacyParseException" in types or any(
        "LegacyParseException" in message for message in messages
    )
    rows.append(
        _row(
            "noise listed as a tombstone",
            tombstone_hit,
            f"{len(tombstones)} tombstone(s); types={sorted(t for t in types if t) or 'none'}",
        )
    )

    new_ok = all(issue_id in unresolved for issue_id in new_ids)
    signal_ok = all(issue_id in unresolved for issue_id in signal_ids)
    # Discard deletes the group, so a surviving issue is still unresolved and not a tombstone.
    rows.append(_row("new issue untouched", new_ok, ", ".join(new_ids)))
    rows.append(_row("signal issue untouched", signal_ok, ", ".join(signal_ids)))

    before, before_reasons = _filtered_quantity(client.outcome_totals(project_id))
    print(f"resending 50 noise events; filtered baseline={before} ({', '.join(before_reasons) or 'none'})")
    resend = subprocess.run(
        [
            sys.executable,
            str(ROOT / "generate.py"),
            "--scenario",
            "noise",
            "--count",
            "50",
            "--allow-empty",
            "--timeout",
            "20",
        ],
        cwd=ROOT,
        check=False,
    )
    if resend.returncode != 0:
        rows.append(_row("resend noise events", False, f"generate.py exited {resend.returncode}"))
    else:
        def _no_new_noise():
            current = _unresolved_ids(client, settings.project)
            fresh = [
                issue
                for issue in client.list_issues(
                    settings.project, query="is:unresolved error.type:LegacyParseException"
                )
            ]
            return (not fresh and noise_gone, current, fresh)

        ok, _current, fresh = _wait_until(_no_new_noise, timeout=60)
        rows.append(
            _row(
                "resend did not store a new noise issue",
                ok,
                "none" if ok else ", ".join(str(issue.get("id")) for issue in fresh),
            )
        )

    def _stats_moved():
        groups = client.outcome_totals(project_id)
        total, reasons = _filtered_quantity(groups)
        return (total > before, total, reasons, groups)

    moved, total, reasons, groups = _wait_until(_stats_moved, timeout=120)
    outcome_detail = ", ".join(reasons) if reasons else "no filtered rows"
    # Also print every outcome so the report shows where the drops landed,
    # including a reason other than discarded-hash if Sentry used one.
    summary = []
    for group in groups:
        by = group.get("by") or {}
        quantity = (group.get("totals") or {}).get("sum(quantity)")
        summary.append(f"{by.get('outcome')}/{by.get('reason') or '-'}={quantity}")
    rows.append(
        _row(
            "dropped events in stats_v2",
            moved,
            f"filtered {before} -> {total}; {outcome_detail}; all={'; '.join(summary) or 'empty'}",
        )
    )

    print()
    print(f"{'check':<42} {'result':<6} detail")
    failed = 0
    for name, result, detail in rows:
        print(f"{name:<42} {result:<6} {detail}")
        if result != "pass":
            failed += 1
    print()
    if failed:
        print(f"{failed} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

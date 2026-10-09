#!/usr/bin/env python3
"""Poll unresolved issues and discard the ones that clear the guardrails.

    python pull_job.py
    python pull_job.py --every 60
    python pull_job.py --consolidate
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict

from noisebot.api import SentryApiError, SentryClient
from noisebot.config import describe, load_settings
from noisebot.guardrails import guardrail_failures
from noisebot.journal import journal
from noisebot.ledger import SqliteLedger
from noisebot.pipeline import consider, consider_cluster, load_normalized
from noisebot.stats import sum_recent_buckets


def _query(min_age_days: int) -> str:
    # age:+Nd means first seen more than N days ago. The guardrail is the exact check.
    # Skipping the token at 0 keeps brand-new sandbox issues in the result set.
    if min_age_days > 0:
        return f"is:unresolved age:+{min_age_days}d"
    return "is:unresolved"


def run_once(settings, client: SentryClient, ledger: SqliteLedger, *, consolidate: bool) -> int:
    latest = client.latest_release_version(settings.project)
    issues = client.list_issues(settings.project, query=_query(settings.min_age_days))
    prepared = []
    for raw in issues:
        stats = (raw.get("stats") or {}).get("24h") or []
        window_count = sum_recent_buckets(stats, settings.window_hours)
        if window_count < settings.threshold:
            journal(
                issue_id=raw.get("id", ""),
                title=raw.get("title"),
                count=window_count,
                decision="skip",
                reasons=["below_threshold"],
            )
            continue
        try:
            normalized = load_normalized(
                raw,
                client=client,
                latest_release=latest,
                window_count=window_count,
            )
        except SentryApiError as exc:
            journal(
                issue_id=raw.get("id", ""),
                title=raw.get("title"),
                count=window_count,
                decision="error",
                reasons=[exc.detail or f"HTTP {exc.status}"],
            )
            continue
        reasons = guardrail_failures(normalized, settings)
        if reasons:
            journal(
                issue_id=normalized["id"],
                title=normalized.get("title"),
                count=window_count,
                decision="skip",
                reasons=reasons,
            )
            continue
        prepared.append(normalized)

    if not consolidate:
        for issue in prepared:
            consider(issue, settings=settings, ledger=ledger, client=client)
        return len(prepared)

    clusters: dict[tuple, list] = defaultdict(list)
    for issue in prepared:
        key = (issue.get("error_type"), issue.get("function"))
        if not key[0] or not key[1]:
            consider(issue, settings=settings, ledger=ledger, client=client)
            continue
        clusters[key].append(issue)
    for group in clusters.values():
        consider_cluster(group, settings=settings, ledger=ledger, client=client)
    return len(prepared)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discard known noisy Sentry issues.")
    parser.add_argument("--every", type=int, default=0, help="Repeat every N seconds. Default: run once.")
    parser.add_argument(
        "--consolidate",
        action="store_true",
        help="Merge issues that share an exception type and crashing function before discarding.",
    )
    args = parser.parse_args(argv)
    settings = load_settings(
        dotenv=True,
        require=("SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_TOKEN"),
    )
    print(describe(settings), file=sys.stderr)
    client = SentryClient(settings)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)

    while True:
        run_once(settings, client, ledger, consolidate=args.consolidate)
        if args.every <= 0:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())

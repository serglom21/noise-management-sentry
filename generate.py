#!/usr/bin/env python3
"""Send sample events for old-release noise, plus two issues that must survive.

    python generate.py --scenario all
    python generate.py --scenario noise --count 300
    python generate.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextvars import ContextVar

import sentry_sdk

from noisebot.api import SentryApiError, SentryClient
from noisebot.config import DEFAULT_STATE, describe, load_settings

_release: ContextVar[str] = ContextVar("noise_release", default="")

NOISE_RELEASES = ("sample-app@6.1.0", "sample-app@6.2.0")
LATEST_RELEASE = "sample-app@7.0.0"

# Same exception type and function, different locale text. Default stack
# grouping may keep them as one issue. The run prints whether Sentry split them.
NOISE_MESSAGES = {
    "en": "Value was missing",
    "pt-BR": "Valor ausente",
    "pt-PT": "Valor em falta",
    "zh": "缺少值",
    "es": "Falta el valor",
}


class LegacyParseException(Exception):
    pass


class SchemaMismatchException(Exception):
    pass


class BufferFlushException(Exception):
    pass


def parse_payload(message: str) -> None:
    raise LegacyParseException(message)


def apply_update() -> None:
    raise SchemaMismatchException("response schema version is not supported")


def flush_buffer() -> None:
    raise BufferFlushException("local buffer overrun")


def _before_send(event, hint):
    release = _release.get()
    if release:
        event["release"] = release
    for value in (event.get("exception") or {}).get("values") or []:
        frames = ((value.get("stacktrace") or {}).get("frames")) or []
        for frame in frames:
            filename = frame.get("filename") or frame.get("abs_path") or ""
            if str(filename).endswith("generate.py"):
                frame["in_app"] = True
    return event


def _capture(release: str, locale: str, func, *args) -> None:
    token = _release.set(release)
    try:
        sentry_sdk.set_tag("locale", locale)
        try:
            func(*args)
        except Exception:
            sentry_sdk.capture_exception()
    finally:
        _release.reset(token)


def send_noise(count: int) -> None:
    locales = list(NOISE_MESSAGES)
    for index in range(count):
        locale = locales[index % len(locales)]
        release = NOISE_RELEASES[index % len(NOISE_RELEASES)]
        _capture(release, locale, parse_payload, NOISE_MESSAGES[locale])
        if (index + 1) % 50 == 0:
            sentry_sdk.flush(timeout=20)


def send_new(count: int) -> None:
    for index in range(count):
        _capture(LATEST_RELEASE, "en", apply_update)
        if (index + 1) % 50 == 0:
            sentry_sdk.flush(timeout=20)


def send_signal(count: int) -> None:
    for index in range(count):
        _capture(LATEST_RELEASE, "en", flush_buffer)
        if (index + 1) % 50 == 0:
            sentry_sdk.flush(timeout=20)


def ensure_releases(client: SentryClient) -> None:
    dated = (
        (NOISE_RELEASES[0], "2024-01-01T00:00:00Z"),
        (NOISE_RELEASES[1], "2024-06-01T00:00:00Z"),
        (LATEST_RELEASE, "2026-01-01T00:00:00Z"),
    )
    for version, date_released in dated:
        client.ensure_release(version, date_released)


def _issues_for(client: SentryClient, project: str, error_type: str) -> list[dict]:
    # error.type matches issues that have at least one event of that exception.
    return client.list_issues(project, query=f"is:unresolved error.type:{error_type}")


def collect(client: SentryClient, project: str) -> dict:
    noise = _issues_for(client, project, "LegacyParseException")
    new = _issues_for(client, project, "SchemaMismatchException")
    signal = _issues_for(client, project, "BufferFlushException")

    def brief(issues: list[dict]) -> list[dict]:
        rows = []
        for issue in issues:
            rows.append(
                {
                    "id": str(issue.get("id")),
                    "title": issue.get("title"),
                    "count": issue.get("count"),
                    "culprit": issue.get("culprit"),
                }
            )
        return rows

    return {
        "noise": brief(noise),
        "new": brief(new),
        "signal": brief(signal),
        "noise_split_by_locale": len(noise) > 1,
    }


def wait_for_issues(client: SentryClient, project: str, wanted: set[str], timeout: int) -> dict:
    deadline = time.time() + timeout
    found = collect(client, project)
    while time.time() < deadline:
        missing = [name for name in wanted if not found.get(name)]
        if not missing:
            return found
        time.sleep(5)
        found = collect(client, project)
    return found


def _print_report(found: dict) -> None:
    for name in ("noise", "new", "signal"):
        rows = found.get(name) or []
        print(f"{name}: {len(rows)} issue(s)")
        for row in rows:
            print(f"  id={row['id']} count={row['count']} title={row['title']}")
    if found.get("noise"):
        if found.get("noise_split_by_locale"):
            print(
                "Localization split the noise into several issues. "
                "Use `python pull_job.py --consolidate` to merge them before discarding. "
                "A fingerprint rule can keep future events in one group; it does not make "
                "an existing discard cover a new hash."
            )
        else:
            print(
                "Localization did not split the noise: one issue for LegacyParseException. "
                "Merge and fingerprint are not required for this run. Discard covers the "
                "shared group hash."
            )


def _merge_state(update: dict) -> None:
    current = {}
    if DEFAULT_STATE.is_file():
        try:
            current = json.loads(DEFAULT_STATE.read_text())
        except json.JSONDecodeError:
            current = {}
    for key in ("noise", "new", "signal"):
        if key in update and update[key]:
            current[key] = update[key]
        elif key in update and key not in current:
            current[key] = update[key]
    if "noise_split_by_locale" in update and update.get("noise"):
        current["noise_split_by_locale"] = update["noise_split_by_locale"]
    DEFAULT_STATE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_STATE.write_text(json.dumps(current, indent=2) + "\n")
    print(f"wrote {DEFAULT_STATE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send sandbox noise, new, and signal events.")
    parser.add_argument("--scenario", choices=("noise", "new", "signal", "all"), default="all")
    parser.add_argument("--count", type=int, default=None, help="Event count for the selected scenario.")
    parser.add_argument("--list", action="store_true", help="Query issue ids and titles, do not send.")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit 0 even if the issues have not shown up yet.",
    )
    parser.add_argument("--timeout", type=int, default=90, help="Seconds to wait for issues to appear.")
    args = parser.parse_args(argv)

    required = ("SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_TOKEN")
    if not args.list:
        required = (*required, "SENTRY_DSN")
    settings = load_settings(dotenv=True, require=required)
    print(describe(settings), file=sys.stderr)
    client = SentryClient(settings)

    if not args.list:
        try:
            ensure_releases(client)
        except SentryApiError as exc:
            print(f"could not create releases: {exc}", file=sys.stderr)
            return 1
        sentry_sdk.init(dsn=settings.dsn, send_default_pii=False, before_send=_before_send)
        noise_count = args.count if args.count is not None else 300
        new_count = args.count if args.scenario == "new" and args.count is not None else 80
        signal_count = args.count if args.scenario == "signal" and args.count is not None else 5
        if args.scenario in {"noise", "all"}:
            print(f"sending {noise_count} noise events on {', '.join(NOISE_RELEASES)}")
            send_noise(noise_count)
        if args.scenario in {"new", "all"}:
            print(f"sending {new_count} new-issue events on {LATEST_RELEASE}")
            send_new(new_count)
        if args.scenario in {"signal", "all"}:
            print(f"sending {signal_count} signal events on {LATEST_RELEASE}")
            send_signal(signal_count)
        sentry_sdk.flush(timeout=30)

    wanted = {"noise", "new", "signal"} if args.scenario == "all" and not args.list else set()
    if args.scenario == "noise":
        wanted = set() if args.allow_empty else {"noise"}
    elif args.scenario == "new":
        wanted = {"new"}
    elif args.scenario == "signal":
        wanted = {"signal"}
    elif args.list:
        wanted = set()

    found = wait_for_issues(client, settings.project, wanted, 0 if args.list else args.timeout)
    _print_report(found)
    _merge_state(found)

    if wanted and any(not found.get(name) for name in wanted) and not args.allow_empty:
        print("timed out waiting for issues to show up in search", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

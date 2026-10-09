import responses

from noisebot.api import SentryClient
from noisebot.ledger import SqliteLedger
from noisebot.stats import sum_recent_buckets
from pull_job import run_once
from tests.conftest import make_settings


def test_threshold_uses_the_newest_hourly_buckets():
    # Oldest first, matching stats["24h"]. The last two hours are 4 and 8.
    stats = [
        [1541455200, 1],
        [1541458800, 2],
        [1541462400, 4],
        [1541466000, 8],
    ]
    assert sum_recent_buckets(stats, 2) == 12
    assert sum_recent_buckets(stats, 24) == 15
    assert sum_recent_buckets(list(reversed(stats)), 1) == 8
    assert sum_recent_buckets([], 3) == 0
    assert sum_recent_buckets(stats, 0) == 0


def _hour_stats(last_count: int) -> list[list[int]]:
    start = 1_700_000_000
    buckets = [[start + hour * 3600, 0] for hour in range(23)]
    buckets.append([start + 23 * 3600, last_count])
    return buckets


def _issue(issue_id: str, count: int) -> dict:
    return {
        "id": issue_id,
        "title": "LegacyParseException: Value was missing",
        "count": str(count),
        "level": "error",
        "firstSeen": "2020-01-01T00:00:00Z",
        "project": {"id": "1", "slug": "noise-repro"},
        "assignedTo": None,
        "metadata": {"type": "LegacyParseException", "function": "parse_payload"},
        "stats": {"24h": _hour_stats(count)},
    }


LATEST_EVENT = {
    "release": "sample-app@6.1.0",
    "entries": [
        {
            "type": "exception",
            "data": {
                "values": [
                    {
                        "type": "LegacyParseException",
                        "value": "Value was missing",
                        "stacktrace": {
                            "frames": [
                                {"function": "sdk_internal", "in_app": False},
                                {"function": "parse_payload", "in_app": True},
                            ]
                        },
                    }
                ]
            },
        }
    ],
}


def _mock_reads(issue: dict):
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/projects/sandbox/noise-repro/",
        json={"id": "1", "slug": "noise-repro", "fingerprintingRules": ""},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/organizations/sandbox/releases/",
        json=[{"version": "sample-app@7.0.0", "dateReleased": "2026-01-01T00:00:00Z"}],
        status=200,
    )
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/organizations/sandbox/issues/",
        json=[issue],
        status=200,
    )
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/organizations/sandbox/issues/10/events/latest/",
        json=LATEST_EVENT,
        status=200,
    )


@responses.activate
def test_dry_run_never_sends_a_put(tmp_path):
    settings = make_settings(tmp_path, dry_run=True, threshold=50, window_hours=3, min_age_days=0)
    _mock_reads(_issue("10", 80))
    client = SentryClient(settings, sleeper=lambda _seconds: None)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    run_once(settings, client, ledger, consolidate=False)
    assert not any(call.request.method == "PUT" for call in responses.calls)
    assert ledger.daily_count() == 1


@responses.activate
def test_below_threshold_does_not_reserve_or_discard(tmp_path):
    settings = make_settings(tmp_path, dry_run=False, threshold=50, window_hours=3, min_age_days=0)
    _mock_reads(_issue("10", 5))
    client = SentryClient(settings, sleeper=lambda _seconds: None)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    run_once(settings, client, ledger, consolidate=False)
    assert ledger.daily_count() == 0
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_latest_release_is_not_discarded_even_above_threshold(tmp_path):
    settings = make_settings(tmp_path, dry_run=False, threshold=50, window_hours=3, min_age_days=0)
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/projects/sandbox/noise-repro/",
        json={"id": "1", "slug": "noise-repro", "fingerprintingRules": ""},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/organizations/sandbox/releases/",
        json=[{"version": "sample-app@7.0.0", "dateReleased": "2026-01-01T00:00:00Z"}],
        status=200,
    )
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/organizations/sandbox/issues/",
        json=[_issue("10", 80)],
        status=200,
    )
    responses.add(
        responses.GET,
        "https://sentry.io/api/0/organizations/sandbox/issues/10/events/latest/",
        json={**LATEST_EVENT, "release": "sample-app@7.0.0"},
        status=200,
    )
    client = SentryClient(settings, sleeper=lambda _seconds: None)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    run_once(settings, client, ledger, consolidate=False)
    assert ledger.daily_count() == 0
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_live_discard_puts_discard_true(tmp_path):
    settings = make_settings(tmp_path, dry_run=False, threshold=50, window_hours=3, min_age_days=0)
    _mock_reads(_issue("10", 80))
    responses.add(
        responses.PUT,
        "https://sentry.io/api/0/projects/sandbox/noise-repro/issues/",
        status=204,
    )
    client = SentryClient(settings, sleeper=lambda _seconds: None)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    run_once(settings, client, ledger, consolidate=False)
    puts = [call for call in responses.calls if call.request.method == "PUT"]
    assert len(puts) == 1
    assert "id=10" in puts[0].request.url
    assert puts[0].request.body == b'{"discard": true}'

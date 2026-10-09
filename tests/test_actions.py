import logging

import pytest
import responses

from noisebot.actions import (
    append_fingerprint_rule,
    discard,
    escape_glob,
    format_fingerprint_rule,
    join_rules,
    rule_present,
)
from noisebot.api import ProjectNotAllowed, SentryClient
from tests.conftest import make_issue, make_settings


def test_glob_escaping():
    function = 'boom*[x]{y}?'
    assert escape_glob(function) == r"boom\*\[x\]\{y\}\?"
    rule = format_fingerprint_rule("LegacyParseException", function, "12")
    assert 'error.type:"LegacyParseException"' in rule
    assert 'app:yes stack.function:"boom\\*\\[x\\]\\{y\\}\\?"' in rule
    assert rule.endswith("-> noise-12")


def test_join_rules_is_idempotent():
    rule = format_fingerprint_rule("LegacyParseException", "parse_payload", "12")
    once = join_rules("", rule)
    twice = join_rules(once, rule)
    assert rule_present(twice, rule)
    assert twice.strip().count(rule.strip()) == 1
    assert join_rules("error.type:Other -> keep\n", rule).count("noise-12") == 1


@responses.activate
def test_append_fingerprint_rule_is_idempotent(tmp_path):
    settings = make_settings(tmp_path, dry_run=False)
    project_url = "https://sentry.io/api/0/projects/sandbox/noise-repro/"
    rule = format_fingerprint_rule("LegacyParseException", "parse_payload", "12")
    responses.add(responses.GET, project_url, json={"id": "1", "fingerprintingRules": ""}, status=200)
    responses.add(responses.PUT, project_url, json={"id": "1", "fingerprintingRules": rule}, status=200)
    responses.add(responses.GET, project_url, json={"id": "1", "fingerprintingRules": rule + "\n"}, status=200)
    client = SentryClient(settings, sleeper=lambda _seconds: None)
    assert append_fingerprint_rule("noise-repro", rule, client=client, settings=settings) == "fingerprint"
    assert append_fingerprint_rule("noise-repro", rule, client=client, settings=settings) == "fingerprint_present"
    puts = [call for call in responses.calls if call.request.method == "PUT"]
    assert len(puts) == 1
    assert b"noise-12" in puts[0].request.body


@responses.activate
def test_dry_run_fingerprint_does_not_put(tmp_path):
    settings = make_settings(tmp_path, dry_run=True)
    project_url = "https://sentry.io/api/0/projects/sandbox/noise-repro/"
    responses.add(responses.GET, project_url, json={"id": "1", "fingerprintingRules": ""}, status=200)
    client = SentryClient(settings, sleeper=lambda _seconds: None)
    rule = format_fingerprint_rule("LegacyParseException", "parse_payload", "12")
    assert append_fingerprint_rule("noise-repro", rule, client=client, settings=settings) == "would_fingerprint"
    assert not any(call.request.method == "PUT" for call in responses.calls)


def test_dry_run_discard_does_not_call_the_client(tmp_path):
    settings = make_settings(tmp_path, dry_run=True)

    class Exploding:
        def discard_issue(self, project, issue_id):
            raise AssertionError("DRY_RUN sent a discard")

    assert discard(make_issue(), client=Exploding(), settings=settings) == "would_discard"


def test_discard_refuses_projects_outside_the_allowlist(tmp_path):
    settings = make_settings(tmp_path, dry_run=False)

    class Exploding:
        def discard_issue(self, project, issue_id):
            raise AssertionError("acted on a project outside the allowlist")

    with pytest.raises(ProjectNotAllowed):
        discard(make_issue(project="other-project"), client=Exploding(), settings=settings)


@responses.activate
def test_retries_429_and_does_not_log_bodies(tmp_path, caplog):
    settings = make_settings(tmp_path)
    url = "https://sentry.io/api/0/projects/sandbox/noise-repro/"
    responses.add(responses.GET, url, json={"detail": "slow"}, status=429, headers={"Retry-After": "0"})
    responses.add(
        responses.GET,
        url,
        json={"id": "1", "fingerprintingRules": "", "exception": "EVENTBODYCANARY"},
        status=200,
    )
    client = SentryClient(settings, sleeper=lambda _seconds: None, max_retries=2)
    with caplog.at_level(logging.INFO, logger="noisebot.api"):
        payload = client.get_project()
    assert payload["id"] == "1"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "EVENTBODYCANARY" not in logged
    assert "slow" not in logged
    assert "-> 429" in logged
    assert "-> 200" in logged

from noisebot.ledger import SqliteLedger
from noisebot.pipeline import consider, consider_cluster
from tests.conftest import make_issue, make_settings


class FakeClient:
    def __init__(self):
        self.discards: list[str] = []
        self.merges: list[list[str]] = []

    def discard_issue(self, project, issue_id):
        self.discards.append(str(issue_id))

    def merge_issues(self, project, issue_ids):
        self.merges.append([str(issue_id) for issue_id in issue_ids])
        return str(issue_ids[0])


def test_same_issue_twice_is_one_action(tmp_path):
    settings = make_settings(tmp_path, dry_run=False, daily_cap=20)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    client = FakeClient()
    issue = make_issue()
    assert consider(issue, settings=settings, ledger=ledger, client=client) == "discard"
    assert consider(issue, settings=settings, ledger=ledger, client=client) == "skip"
    assert client.discards == ["10"]


def test_daily_cap(tmp_path):
    settings = make_settings(tmp_path, dry_run=False, daily_cap=1)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    client = FakeClient()
    first = consider(make_issue(id="10"), settings=settings, ledger=ledger, client=client)
    second = consider(make_issue(id="11"), settings=settings, ledger=ledger, client=client)
    assert first == "discard"
    assert second == "skip"
    assert client.discards == ["10"]
    assert ledger.daily_count() == 1


def test_failed_discard_can_be_retried(tmp_path):
    settings = make_settings(tmp_path, dry_run=False, daily_cap=5)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)

    class Flaky(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def discard_issue(self, project, issue_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            super().discard_issue(project, issue_id)

    client = Flaky()
    assert consider(make_issue(), settings=settings, ledger=ledger, client=client) == "error"
    assert consider(make_issue(), settings=settings, ledger=ledger, client=client) == "discard"
    assert client.discards == ["10"]


def test_consolidate_merges_then_discards_parent(tmp_path):
    settings = make_settings(tmp_path, dry_run=False, daily_cap=5)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    client = FakeClient()
    issues = [
        make_issue(id="10", title="en"),
        make_issue(id="11", title="pt"),
    ]
    decision = consider_cluster(issues, settings=settings, ledger=ledger, client=client)
    assert decision == "discard"
    assert client.merges == [["10", "11"]]
    assert client.discards == ["10"]
    assert ledger.daily_count() == 2

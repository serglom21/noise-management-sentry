# Sentry noise automation (sandbox repro)

A small Python proof of concept that discards already-known issues from old client releases so they stop consuming Sentry quota. It will not discard a new issue, a fatal issue, or an issue whose event is from the project's latest release.

Use it only on an organization you own. Do not point `SENTRY_ORG`, the DSN, or the token at an organization you do not control.

API behavior was checked against [docs.sentry.io](https://docs.sentry.io) and `getsentry/sentry`. Differences from the original notes are in [VERIFIED.md](VERIFIED.md).

## What it does

1. `generate.py` sends three kinds of events with `sentry-sdk`:
   - **Noise.** `LegacyParseException` from `parse_payload`, localized in en / pt-BR / pt-PT / zh / es, on `sample-app@6.1.0` and `sample-app@6.2.0`. Default 300 events.
   - **New issue.** `SchemaMismatchException` from `apply_update` on `sample-app@7.0.0`. Default 80 events, enough to pass the default threshold. This one must stay.
   - **Signal.** `BufferFlushException` from `flush_buffer` on `sample-app@7.0.0`. Default 5 events, under the threshold. This one must stay.
2. `pull_job.py` lists unresolved issues, sums the last `WINDOW_HOURS` of `stats["24h"]`, and discards issues that clear the guardrails and the ledger.
3. `webhook_app.py` does the same when an issue alert fires. The signature is HMAC-SHA256 of the raw body, compared with `hmac.compare_digest`.

Discard calls `PUT /api/0/projects/{org}/{project}/issues/?id={id}` with `{"discard": true}`. That tombstones the group hash. It does not install an error-message inbound filter. Later events with the same hash are dropped and show up in stats as outcome `filtered`, reason `discarded-hash`.

`DRY_RUN` defaults to **true**. A live run has to opt in.

## Setup

Python 3.11 or newer.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Create an **internal integration** on the sandbox org (Settings → Developer Settings → Create New Integration → Internal Integration):

| Permission | Level | Why |
| --- | --- | --- |
| Issue & Event | **Admin** | Discard checks `event:admin`. `event:write` can call the endpoint and still be rejected. |
| Project | Write | Read the project and update fingerprint rules. |
| Release | Admin | Create the `sample-app@…` releases. |
| Organization | Read | `stats_v2` and the team list. |
| Team | Read | `bootstrap.py` creates the project on a team. |

Copy the token into `SENTRY_TOKEN` and the client secret into `SENTRY_CLIENT_SECRET`.

`SENTRY_HOST` defaults to `https://sentry.io`. For a region pin set `https://us.sentry.io` (or `de.sentry.io`, and so on). That is the API host, not the ingest host inside the DSN.

The org must have the `projects:discard-groups` feature. If discard returns `You do not have that feature enabled`, the plan cannot tombstone issues and this repro cannot finish the live check.

```bash
python bootstrap.py
```

Paste the printed DSN into `SENTRY_DSN`. The project slug defaults to `noise-repro`. Use a fresh project so `sample-app@7.0.0` is actually the latest release.

## Run order

```bash
# 1. Send events. This also creates the three releases, with 7.0.0 dated last.
python generate.py --scenario all

# 2. Preview. DRY_RUN is true unless .env says otherwise.
python pull_job.py

# 3. Live discard. Issues from this run are minutes old, so the age floor has to be 0.
DRY_RUN=false MIN_AGE_DAYS=0 python pull_job.py

# 4. Confirm noise is gone, the resend is dropped, and new/signal are still unresolved.
DRY_RUN=false MIN_AGE_DAYS=0 python verify.py
```

`--count` sets the noise volume (default 300). `python generate.py --scenario new --count 10` sends 10 new-issue events instead. `--scenario signal` defaults to 5. `python generate.py --list` only prints current issue ids and titles.

If several issues share `LegacyParseException` and `parse_payload`, localization split them. The generator says so. Merge before discarding:

```bash
DRY_RUN=false MIN_AGE_DAYS=0 python pull_job.py --consolidate
```

Repeat the pull on a timer with `--every 60`.

Each decision is one JSON line on stdout: issue id, title, count, decision (`would_discard`, `discard`, `skip`, `error`, …), and reasons. Nothing in those lines is an event body.

The ledger is `state/ledger.sqlite`. One row per issue, at most `DAILY_CAP` per UTC day (default 20). A failed API call releases the row so the next run can retry.

## Guardrails

An issue is skipped when any of these are true:

- Its project slug is not in `ALLOWED_PROJECTS` (defaults to `SENTRY_PROJECT`). The discard, merge, and fingerprint calls check this again before any `PUT`.
- `firstSeen` is missing or newer than `MIN_AGE_DAYS` (default 7).
- The event release equals the latest project release (greatest `dateReleased`, then `dateCreated`). A missing release is also skipped.
- `level` is `fatal`.
- `REQUIRE_OWNER=true` and the issue has no assignee.

The pull job also skips issues whose recent hourly count is under `THRESHOLD` (default 50) inside `WINDOW_HOURS` (default 24).

## Webhook

```bash
uvicorn webhook_app:app --port 8000
```

Expose it with either:

```bash
cloudflared tunnel --url http://localhost:8000
# or
ngrok http 8000
```

On the internal integration, set the webhook URL to `https://<tunnel>/sentry`, enable **Alert Rule Action**, and save. Then create an issue alert:

- When the number of events in an issue is more than N in 1 hour. Use the same N as `THRESHOLD`.
- Filter: the event is not from the latest release.
- Action: this integration.
- Action interval: 24 hours.

Sentry signs the raw body with the client secret in `Sentry-Hook-Signature` and sets `Sentry-Hook-Resource: event_alert`. A bad signature returns 401. Anything that is not `event_alert` is acknowledged and ignored. The HTTP handler returns as soon as the signature checks out; the discard runs on a FastAPI background task so the call stays inside Sentry's one-second webhook timeout.

`handler(event, context)` is the Lambda wrapper around the same function. Lambda runs the work inline, because a background task would be frozen when the response is returned.

`SAVE_WEBHOOK_SAMPLE=true` writes **one** raw body to `samples/event_alert.json` so you can confirm field names (the issue id is read from `data.event.issue_id`). **Sandbox only.** That payload contains event data. The file is gitignored. Leave the flag false and delete the file when you are done looking at it.

## Fingerprints

`noisebot.actions.build_fingerprint_rule` builds a rule from the last in-app frame:

```text
error.type:"LegacyParseException" app:yes stack.function:"parse_payload" -> noise-<issue id>
```

`append_fingerprint_rule` adds it to the project only if that exact line is not already there. Glob characters in the type and function are escaped.

A fingerprint rule changes the hash of **future** events. The discard tombstone only matches the hashes it already has, so adding a rule does not make the old discard cover the new hash. Use `--consolidate` to merge an existing split, then discard. Add a fingerprint only when you want later events to land in one group before a later discard.

## Reset the sandbox

```bash
python bootstrap.py --reset
rm -rf state samples/event_alert.json
```

`--reset` deletes `SENTRY_PROJECT` and creates it again. It refuses to delete a project that is not in `ALLOWED_PROJECTS`. Then run `generate.py` again. Deleting the project removes issues, tombstones, releases, and fingerprint rules for that project only.

## Tests

No network. HTTP is mocked with `responses`.

```bash
pytest
```

Covers the signature (valid, tampered, and a non-`event_alert` resource), each guardrail, ledger dedupe and the daily cap, dry-run doing no `PUT`, fingerprint escaping and idempotent append, and the hourly threshold sum.

## Layout

| Path | Role |
| --- | --- |
| `generate.py` | Send the three scenarios and print issue ids. |
| `pull_job.py` | Poll, threshold, guardrails, discard. |
| `webhook_app.py` | FastAPI `POST /sentry` and the Lambda `handler`. |
| `verify.py` | Live pass/fail table after a real discard. |
| `bootstrap.py` | Create or reset the sandbox project and print the DSN. |
| `noisebot/` | API client, guardrails, SQLite ledger, actions. |
| `VERIFIED.md` | The seven API claims, with sources. |

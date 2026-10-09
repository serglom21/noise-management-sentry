# Webhook handler

One process receives a Sentry issue alert and discards the issue when the guardrails allow it. Run `webhook_app.py`. This path does not use the event generator, the pull job, or the live-check script.

The handler verifies the signature, reads the issue id, and calls Sentry. Each decision is one JSON line on stdout: issue id, title, count, decision, and reasons. Those lines do not include event bodies.

`DRY_RUN` defaults to **true**. A live discard has to opt in.

## What it does on each alert

1. `POST /sentry` checks `Sentry-Hook-Signature`, an HMAC-SHA256 of the raw body, using the integration client secret and `hmac.compare_digest`. A bad signature returns 401.
2. Anything other than `Sentry-Hook-Resource: event_alert` is acknowledged and ignored.
3. The issue id is read from `data.event.issue_id`.
4. The handler returns 202 as soon as the signature checks out. The discard runs on a background task so the call stays inside Sentry's one-second webhook timeout.
5. Guardrails run before any `PUT`. An empty result means the issue may be discarded. Any of these skips it:
   - The project slug is not in `ALLOWED_PROJECTS`.
   - `firstSeen` is missing or newer than `MIN_AGE_DAYS` (default 7).
   - The event release is missing, or the project's latest release is missing. Latest means the greatest `dateReleased`, then `dateCreated`.
   - The event release equals that latest release.
   - `level` is `fatal`.
   - `REQUIRE_OWNER=true` and the issue has no assignee.
6. A SQLite ledger (`state/ledger.sqlite`) records one action per issue, at most `DAILY_CAP` per UTC day (default 20). A failed API call releases the row so the next alert can retry.
7. Discard is `PUT /api/0/projects/{org}/{project}/issues/?id={id}` with `{"discard": true}`. That tombstones the group hash. Later events with the same hash are dropped and show up in stats as outcome `filtered`, reason `discarded-hash`.

The alert is the volume check. The handler does not apply `THRESHOLD` again, because hourly stats lag behind the alert that just fired.

With `DRY_RUN=true` the log says `would_discard` and nothing is changed. With `DRY_RUN=false` the log says `discard`.

This handler does not write fingerprint rules and does not merge issues. A fingerprint rule changes the hash of later events. The tombstone only matches hashes it already has, so a new rule does not extend an existing discard.

## Run the handler

Python 3.11 or newer.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set these in `.env`:

| Variable | Purpose |
| --- | --- |
| `SENTRY_ORG` | Organization slug. |
| `SENTRY_PROJECT` | Project slug the handler is allowed to change. |
| `SENTRY_TOKEN` | Internal integration token. |
| `SENTRY_CLIENT_SECRET` | Same integration's client secret. Used only to verify the signature. |
| `SENTRY_HOST` | API host. Default `https://sentry.io`. For a region pin use `https://us.sentry.io` (or `de.sentry.io`, and so on). This is not the ingest host inside a DSN. |
| `ALLOWED_PROJECTS` | Comma-separated project slugs. Defaults to `SENTRY_PROJECT`. |
| `DRY_RUN` | `true` until you have read a `would_discard` line you agree with. |
| `MIN_AGE_DAYS` | Age floor. `0` allows an issue that was first seen today. |
| `DAILY_CAP` | Maximum recorded actions per UTC day. |
| `REQUIRE_OWNER` | `true` skips issues with no assignee. |

`SENTRY_DSN` is not used by the handler.

The organization must have the `projects:discard-groups` feature. If discard returns `You do not have that feature enabled`, the plan cannot tombstone issues.

```bash
uvicorn webhook_app:app --port 8000
```

Sentry must reach `https://<your host>/sentry` on the public internet. For a local trial:

```bash
cloudflared tunnel --url http://localhost:8000
# or
ngrok http 8000
```

Use the tunnel host only in the integration URL. Leave the app bound to port 8000.

`handler(event, context)` in the same file is the Lambda wrapper. Lambda runs the discard inline, because a background task would be frozen when the response is returned.

## Create the Sentry integration

In the organization: **Settings → Developer Settings → Create New Integration → Internal Integration**.

| Permission | Level | Why |
| --- | --- | --- |
| Issue & Event | **Admin** | Discard checks `event:admin`. `event:write` can call the endpoint and still be rejected. |
| Project | Read | Resolve the project when loading the latest release. |
| Release | Read | List releases so the handler can find the latest one. |
| Organization | Read | The release list is an organization endpoint. |

Webhook URL: `https://<your host>/sentry`.

Enable **Alert Rule Action** and save. Copy the token into `SENTRY_TOKEN` and the client secret into `SENTRY_CLIENT_SECRET`. Restart the handler after changing `.env`.

## Create the issue alert

Go to **Monitors → Alerts → Create Alert**. The volume filter only works with certain triggers, and the latest-release filter cannot be negated on its own. Build the alert as follows.

1. **Source.** Alert on all issues in the project named by `SENTRY_PROJECT`.
2. **Environment.** All environments, unless you want one environment only.
3. **When.** Choose **An event or issue activity is captured**. The "number of events" filter is marked unavailable on the other triggers.
4. **If.** Leave the block on **all**. Add one filter: **Number of events**, **more than** your quota threshold, **in one hour**. Use the same number you would have put in `THRESHOLD`.
5. Do not add **Latest release**. That filter only matches events that are from the latest release, and it has no "is not" option. Setting the whole block to **none** would also invert the volume filter, so a high-volume issue would stop matching. The handler skips latest-release events itself (`latest_release` in the log).
6. **Then.** **Send a notification via an integration**, and choose the internal integration from the previous step.
7. **Throttling.** 24 hours. Sentry will not call the handler again for that issue until the window passes.
8. Name the alert and save.

Send a test notification from the alert page to confirm the integration is selected. A test event may not carry a real issue id; a real firing alert is the check that matters.

## Read the log

After an issue crosses the hourly count, the handler prints one line:

```json
{"issue_id": "123", "title": "LegacyParseException: Value was missing", "count": 80, "decision": "discard", "reasons": ["event_alert"]}
```

| Decision | Meaning |
| --- | --- |
| `would_discard` | Guardrails passed and `DRY_RUN` is still true. |
| `discard` | The issue was tombstoned. |
| `skip` | A guardrail, the ledger, or the daily cap stopped it. `reasons` says which. |
| `error` | The Sentry call failed. The ledger row was released. |

Start with `DRY_RUN=true`. When the old-release issue logs `would_discard` and the current-release issue logs `skip` with `latest_release`, set `DRY_RUN=false` and restart the handler. The next alert for an issue that has not been reserved yet will discard it.

A second alert for an issue already in the ledger logs `skip` / `already_actioned`. That row is the record that the issue was already handled.

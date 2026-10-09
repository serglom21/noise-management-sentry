# API verification

Checked on 2026-10-08 against [docs.sentry.io](https://docs.sentry.io) and `getsentry/sentry` `master`. Where a claim was wrong or incomplete, the code follows the source and the difference is noted.

## 1. Bulk discard — ✅, with two corrections

`PUT /api/0/projects/{org}/{project}/issues/?id={id}` accepts `{"discard": true}`.

The published [project bulk-mutate docs](https://docs.sentry.io/api/events/bulk-mutate-a-list-of-issues/) omit `discard` from the body parameters. The [organization bulk-mutate docs](https://docs.sentry.io/api/events/bulk-mutate-an-organizations-issues/) include it. Both endpoints call the same helper, and the project `PUT` is not a different implementation:

- [`ProjectGroupIndexEndpoint.put`](https://github.com/getsentry/sentry/blob/master/src/sentry/issues/endpoints/project_group_index.py) calls `update_groups_with_search_fn`.
- [`update_groups`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/helpers/group_index/update.py) reads `discard` from the validated body and calls `handle_discard`.
- [`GroupValidator`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/helpers/group_index/validators/group.py) declares `discard = BooleanField` and is constructed with `partial=True`, so a body that is only `{"discard": true}` is valid. Any other field sent alongside `discard` is rejected.

**Minimum permission is `event:admin`, not `event:write`.** The project endpoint allows `PUT` with `event:write` or `event:admin` ([`ProjectEventPermission`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/bases/project.py)), but `validate_discard` then requires `access.has_scope("event:admin")` and returns *"You do not have permission to discard events"* otherwise. On an internal integration, set **Issue & Event** to **Admin**.

**Plan feature.** `handle_discard` returns HTTP 400 *"You do not have that feature enabled"* unless `projects:discard-groups` is on for the project. That flag is not enabled for every plan. A sandbox on a plan without it cannot complete the live discard.

**What discard actually does.** It creates a `GroupTombstone`, points the group's hashes at that tombstone, and deletes the group. It does **not** install an error-message inbound filter. Later events with the same group hash are dropped during grouping. See claim 7.

The code still calls the project endpoint from the design, with body `{"discard": true}`.

## 2. Bulk merge — ✅, `merge: 1` is a boolean

The same project `PUT` accepts `{"merge": 1}` plus repeated `id` query parameters.

`merge` is a `BooleanField`. Django REST Framework treats `1` as true. It is not the id of the parent issue. The parent is chosen inside `handle_merge`. The response body (not the request) is shaped like `{"merge": {"parent": "10", "children": ["11", "12"]}}`. At least two ids are required; a single id is a no-op. Merging across projects is rejected.

Sources: the same [`GroupValidator`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/helpers/group_index/validators/group.py) and [`prepare_response`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/helpers/group_index/update.py) (`if result.get("merge") and len(group_list) > 1`). The project docs describe `merge` as a boolean: [Bulk Mutate a List of Issues](https://docs.sentry.io/api/events/bulk-mutate-a-list-of-issues/).

The code sends `{"merge": 1}` and then discards the returned parent.

## 3. Project fingerprinting rules — ✅

`GET /api/0/projects/{org}/{project}/` returns `fingerprintingRules` (a newline-delimited string, possibly empty). `PUT` on the same path with `{"fingerprintingRules": "..."}` updates `sentry:fingerprinting_rules`.

- Serializer field: [`project.py`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/serializers/models/project.py) (`fingerprintingRules`).
- Update path and validation: [`project_details.py`](https://github.com/getsentry/sentry/blob/master/src/sentry/core/endpoints/project_details.py). The value is parsed with `FingerprintingConfig.from_config_string`. Invalid rules are a 400. `PUT` requires `project:write` or `project:admin` (internal integration: **Project: Write**).

Rule syntax used by this repo, from [Fingerprint Rules](https://docs.sentry.io/concepts/data-management/event-grouping/fingerprint-rules/):

```text
error.type:"LegacyParseException" app:yes stack.function:"parse_payload" -> noise-123
```

`app:yes` and `stack.function` must match the **same** frame. There is no matcher for "last frame only". The code takes the function from the last in-app frame and puts that name in the rule. Glob metacharacters (`* ? [ ] { }`) are escaped. Quoted values are unescaped with `unescape_string` before the glob runs ([parser](https://github.com/getsentry/sentry/blob/master/src/sentry/grouping/fingerprinting/parser.py)).

Fingerprint rules apply to **future** events and change their hash. A tombstone only matches the hashes it was given, so a new fingerprint does not make an existing discard cover the new hash. Discard is what stops quota use. The fingerprint helper is for collapsing a localized split before a later discard.

## 4. Latest event — ✅

`GET /api/0/organizations/{org}/issues/{id}/events/latest/` exists.

[`create_group_urls`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/urls.py) registers `events/(latest|oldest|recommended|{id})/` and is mounted at `organizations/{org}/issues/`. The same route also exists at the legacy `/api/0/issues/{id}/events/latest/`.

## 5. Issue search, sort, and hourly stats — ✅

`GET /api/0/organizations/{org}/issues/` supports all three.

| Claim | Result |
| --- | --- |
| `query=is:unresolved age:+30d` | `age:+30d` means first seen **more than** 30 days ago. `age:-30d` means first seen within 30 days. [Issue properties](https://docs.sentry.io/concepts/search/searchable-properties/issues/). |
| `sort=freq` | Documented sort value ("Events"). [List an Organization's Issues](https://docs.sentry.io/api/events/list-an-organizations-issues/). |
| `groupStatsPeriod=24h` | Allowed values are `24h`, `14d`, and `auto`. The response `stats["24h"]` is 24 hourly `[unix_ts, count]` pairs, oldest first. Confirmed by the docs example and [`StreamGroupSerializerSnuba`](https://github.com/getsentry/sentry/blob/master/src/sentry/api/serializers/models/group_stream.py) (`"24h": StatsPeriod(24, timedelta(hours=1))`). The code sorts by timestamp anyway. |

The query parameter is `groupStatsPeriod`. `statsPeriod` is a different parameter (the search window) and is not required for the buckets.

Read scope is `event:read`, `event:write`, or `event:admin`.

## 6. Webhook signature and `event_alert` — ✅, verify the raw body

Headers, from [Webhooks](https://docs.sentry.io/integrations/integration-platform/webhooks/) and [`AppPlatformEvent.sentry_headers`](https://github.com/getsentry/sentry/blob/master/src/sentry/sentry_apps/api/serializers/app_platform_event.py):

- `Sentry-Hook-Resource: event_alert`
- `Sentry-Hook-Signature`: hex HMAC-SHA256 of the exact body string, keyed with the integration client secret

[`SentryApp.build_signature`](https://github.com/getsentry/sentry/blob/master/src/sentry/sentry_apps/models/sentry_app.py):

```python
hmac.new(key=secret.encode("utf-8"), msg=body.encode("utf-8"), digestmod=sha256).hexdigest()
```

`body` is the JSON string Sentry actually sends (`json.dumps` of the payload). Re-serializing a parsed body will not match. The JavaScript sample on the docs page uses `JSON.stringify(request.body)`, which is the wrong input. This repo verifies `hmac.compare_digest` against the **raw** request bytes.

The issue id for `event_alert` is `data.event.issue_id` ([Issue Alerts](https://docs.sentry.io/integrations/integration-platform/webhooks/issue-alerts/)). The handler also accepts `data.issue.id` if a payload uses the issue-webhook shape. `SAVE_WEBHOOK_SAMPLE=true` writes one raw body to `samples/` so the sandbox payload can be checked. That file contains event data; keep it sandbox-only and do not commit it.

Sentry expects a webhook response within about one second. The FastAPI app returns after queueing a background task.

## 7. Error-message inbound filter — ✅, and it is not what discard uses

[Inbound Filters](https://docs.sentry.io/concepts/data-management/filtering/): on error events the legacy error-message filter matches the whole description `{exception.type}: {exception.value}`. That is the same shape as `{ExceptionType}: {message}`. Wildcards are recommended (`*LegacyParseException*`). Transactions are not matched. The filter runs on the ingested event, before deobfuscation.

Discard does not write that filter. Discarded events are counted under outcome **`filtered`** with reason **`discarded-hash`** (`FilterStatKeys.DISCARDED_HASH` in [`inbound_filters.py`](https://github.com/getsentry/sentry/blob/master/src/sentry/ingest/inbound_filters.py)). `verify.py` queries [`/organizations/{org}/stats_v2/`](https://docs.sentry.io/api/organization/retrieve-event-counts-for-an-organization-v2/) grouped by `outcome` and `reason` and reports the buckets it actually sees.

Discarded groups are listed at `GET /api/0/projects/{org}/{project}/tombstones/` ([endpoint](https://github.com/getsentry/sentry/blob/master/src/sentry/issues/endpoints/group_tombstone.py), mounted in `urls.py`). The serializer does **not** include `previous_group_id`. `verify.py` treats a missing unresolved issue plus a tombstone whose `metadata.type` matches as the discard record.

## Code changes made because of this

| Design note | What the code does |
| --- | --- |
| Minimum permission left unspecified | Requires **Issue & Event: Admin** (`event:admin`). `event:write` can call the endpoint and still fail the discard check. |
| Discard drops events via the error-message filter | Discard tombstones the group hash. The error-message filter is a separate project setting and is not written. |
| `{"merge": 1}` as the merge request | Sent as specified. `1` is boolean true. The parent id is read from the response. |
| Signature over the raw body | Implemented that way, not the docs' `JSON.stringify` sample. |
| Fingerprint the crashing frame | Rule uses `error.type` plus `app:yes stack.function` of the last in-app frame. Documented that this does not extend an existing tombstone to the new hash. |

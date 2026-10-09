"""Small Sentry API client.

Retries 429 and 5xx. Logs the method, path, and status only. Request and
response bodies are never logged: event payloads and issue titles can land in
either one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests

from noisebot.config import Settings

logger = logging.getLogger("noisebot.api")

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class SentryApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: str = ""):
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail
        message = f"{method} {path} failed with HTTP {status}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class ProjectNotAllowed(RuntimeError):
    def __init__(self, project: str):
        self.project = project
        super().__init__(f"Refusing to act on project {project!r}: it is not in ALLOWED_PROJECTS.")


def _safe_detail(response: requests.Response) -> str:
    """A short API error string. Anything else is dropped, including event bodies."""

    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail[:300]
    if isinstance(detail, list) and detail and isinstance(detail[0], str):
        return str(detail[0])[:300]
    return ""


class SentryClient:
    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
    ):
        self.settings = settings
        self.sleeper = sleeper
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json",
                "User-Agent": "noise-repro/0.1",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
        allow_statuses: set[int] | None = None,
    ) -> requests.Response:
        url = self.settings.api_base + path
        # Path only. Query strings can carry issue searches; bodies can carry events.
        logger.info("sentry_api %s %s", method, path)
        response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            response = self.session.request(method, url, params=params, json=json, timeout=30)
            logger.info("sentry_api %s %s -> %s", method, path, response.status_code)
            if response.status_code not in _RETRY_STATUSES or attempt == self.max_retries:
                break
            delay = _retry_delay(response, attempt)
            self.sleeper(delay)
        assert response is not None
        allowed = allow_statuses or set()
        if response.status_code >= 400 and response.status_code not in allowed:
            raise SentryApiError(method, path, response.status_code, _safe_detail(response))
        return response

    def get_project(self, project: str | None = None) -> dict:
        project = project or self.settings.project
        response = self.request("GET", f"/projects/{self.settings.org}/{project}/")
        return response.json()

    def update_project(self, project: str, body: dict) -> dict:
        self._check_project(project)
        response = self.request("PUT", f"/projects/{self.settings.org}/{project}/", json=body)
        return response.json()

    def list_issues(self, project: str | None = None, *, query: str, limit: int = 100) -> list[dict]:
        project = project or self.settings.project
        path = f"/organizations/{self.settings.org}/issues/"
        params: dict[str, Any] = {
            "query": query,
            "sort": "freq",
            "groupStatsPeriod": "24h",
            "limit": limit,
            "project": project,
        }
        issues: list[dict] = []
        seen_cursors: set[str] = set()
        while True:
            response = self.request("GET", path, params=params)
            page = response.json()
            if isinstance(page, list):
                issues.extend(page)
            cursor = _next_cursor(response.headers.get("Link", ""))
            if not cursor or cursor in seen_cursors or len(issues) >= 500:
                break
            seen_cursors.add(cursor)
            params = {**params, "cursor": cursor}
        return issues

    def get_issue_with_stats(self, issue_id: str) -> dict | None:
        path = f"/organizations/{self.settings.org}/issues/"
        response = self.request(
            "GET",
            path,
            params={"group": str(issue_id), "groupStatsPeriod": "24h", "query": ""},
            allow_statuses={404},
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        if isinstance(payload, list):
            return payload[0] if payload else None
        return payload

    def get_issue(self, issue_id: str) -> dict | None:
        path = f"/organizations/{self.settings.org}/issues/{issue_id}/"
        response = self.request("GET", path, allow_statuses={404})
        if response.status_code == 404:
            return None
        return response.json()

    def latest_event(self, issue_id: str) -> dict | None:
        path = f"/organizations/{self.settings.org}/issues/{issue_id}/events/latest/"
        response = self.request("GET", path, allow_statuses={404})
        if response.status_code == 404:
            return None
        return response.json()

    def latest_release_version(self, project: str | None = None) -> str | None:
        """Release with the greatest dateReleased, then dateCreated.

        The sample run creates ``sample-app@7.0.0`` with the newest ``dateReleased``,
        which is also the greatest semver, so it matches Sentry's latest-release
        check either way.
        """

        project = project or self.settings.project
        project_payload = self.get_project(project)
        project_id = project_payload.get("id")
        path = f"/organizations/{self.settings.org}/releases/"
        response = self.request("GET", path, params={"project": project_id, "per_page": 100})
        releases = response.json()
        if not isinstance(releases, list) or not releases:
            return None

        def sort_key(release: dict) -> str:
            return str(release.get("dateReleased") or release.get("dateCreated") or "")

        winner = max(releases, key=sort_key)
        version = winner.get("version")
        return str(version) if version else None

    def discard_issue(self, project: str, issue_id: str) -> None:
        self._check_project(project)
        path = f"/projects/{self.settings.org}/{project}/issues/"
        self.request("PUT", path, params=[("id", str(issue_id))], json={"discard": True})

    def merge_issues(self, project: str, issue_ids: list[str]) -> str:
        """Merge and return the parent id Sentry chose. ``merge: 1`` is boolean true."""

        self._check_project(project)
        if len(issue_ids) < 2:
            raise SentryApiError("PUT", "issues", 400, "merge requires at least two issue ids")
        path = f"/projects/{self.settings.org}/{project}/issues/"
        response = self.request(
            "PUT",
            path,
            params=[("id", str(issue_id)) for issue_id in issue_ids],
            json={"merge": 1},
        )
        if response.status_code == 204 or not response.content:
            raise SentryApiError("PUT", path, response.status_code, "merge returned no parent")
        payload = response.json()
        parent = (payload.get("merge") or {}).get("parent") if isinstance(payload, dict) else None
        if not parent:
            raise SentryApiError("PUT", path, response.status_code, "merge response had no parent")
        return str(parent)

    def list_tombstones(self, project: str | None = None) -> list[dict]:
        project = project or self.settings.project
        path = f"/projects/{self.settings.org}/{project}/tombstones/"
        response = self.request("GET", path, allow_statuses={404})
        if response.status_code == 404:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def outcome_totals(self, project_id: str, *, stats_period: str = "24h") -> list[dict]:
        path = f"/organizations/{self.settings.org}/stats_v2/"
        params = [
            ("field", "sum(quantity)"),
            ("groupBy", "outcome"),
            ("groupBy", "reason"),
            ("category", "error"),
            ("statsPeriod", stats_period),
            ("interval", "1h"),
            ("project", str(project_id)),
        ]
        response = self.request("GET", path, params=params)
        payload = response.json()
        groups = payload.get("groups") if isinstance(payload, dict) else None
        return groups if isinstance(groups, list) else []

    def ensure_release(self, version: str, date_released: str, project: str | None = None) -> None:
        project = project or self.settings.project
        path = f"/organizations/{self.settings.org}/releases/"
        response = self.request(
            "POST",
            path,
            json={"version": version, "projects": [project], "dateReleased": date_released},
            allow_statuses={400, 409},
        )
        if response.status_code < 400:
            return
        encoded = quote(version, safe="")
        existing = self.request(
            "GET",
            f"/organizations/{self.settings.org}/releases/{encoded}/",
            allow_statuses={404},
        )
        if existing.status_code == 404:
            raise SentryApiError("POST", path, response.status_code, _safe_detail(response))

    def list_teams(self) -> list[dict]:
        response = self.request("GET", f"/organizations/{self.settings.org}/teams/")
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def create_project(self, team: str, slug: str) -> dict:
        self._check_project(slug)
        response = self.request(
            "POST",
            f"/teams/{self.settings.org}/{team}/projects/",
            json={"name": slug, "slug": slug, "platform": "python"},
        )
        return response.json()

    def delete_project(self, project: str) -> None:
        self._check_project(project)
        self.request(
            "DELETE",
            f"/projects/{self.settings.org}/{project}/",
            allow_statuses={404},
        )

    def client_dsn(self, project: str | None = None) -> str | None:
        project = project or self.settings.project
        response = self.request("GET", f"/projects/{self.settings.org}/{project}/keys/")
        keys = response.json()
        if not isinstance(keys, list) or not keys:
            return None
        dsn = keys[0].get("dsn")
        if isinstance(dsn, dict):
            return dsn.get("public")
        if isinstance(dsn, str):
            return dsn
        return None

    def _check_project(self, project: str) -> None:
        if project not in self.settings.allowed_projects:
            raise ProjectNotAllowed(project)


def _retry_delay(response: requests.Response, attempt: int) -> float:
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
    return min(0.5 * (2**attempt), 30.0)


def _next_cursor(link_header: str) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' not in part or 'results="true"' not in part:
            continue
        url = part.split(";", 1)[0].strip().lstrip("<").rstrip(">")
        cursor = parse_qs(urlparse(url).query).get("cursor", [None])[0]
        return cursor
    return None

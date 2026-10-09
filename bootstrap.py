#!/usr/bin/env python3
"""Create the sandbox project ``noise-repro`` and print its DSN.

    python bootstrap.py
    python bootstrap.py --reset

``--reset`` deletes that project first. It only deletes a project that is in
ALLOWED_PROJECTS, which defaults to SENTRY_PROJECT.
"""

from __future__ import annotations

import argparse
import sys

from noisebot.api import SentryApiError, SentryClient
from noisebot.config import describe, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the sandbox noise-repro project.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the allowlisted project, then create it again.",
    )
    args = parser.parse_args(argv)
    settings = load_settings(dotenv=True, require=("SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_TOKEN"))
    print(describe(settings), file=sys.stderr)
    client = SentryClient(settings)
    project = settings.project

    if args.reset:
        print(f"deleting project {project}")
        client.delete_project(project)

    try:
        client.get_project(project)
        print(f"project {project} already exists")
    except SentryApiError as exc:
        if exc.status != 404:
            print(str(exc), file=sys.stderr)
            return 1
        teams = client.list_teams()
        if not teams:
            print("the org has no teams; create one in Sentry, then re-run", file=sys.stderr)
            return 1
        team = settings.team
        slugs = [item.get("slug") for item in teams]
        if team is None:
            if len(slugs) > 1:
                print(
                    "set SENTRY_TEAM to one of: " + ", ".join(slug for slug in slugs if slug),
                    file=sys.stderr,
                )
                return 1
            team = slugs[0]
        elif team not in slugs:
            print(f"SENTRY_TEAM {team} is not in this org", file=sys.stderr)
            return 1
        print(f"creating project {project} on team {team}")
        client.create_project(team, project)

    dsn = client.client_dsn(project)
    if not dsn:
        print("project has no client key", file=sys.stderr)
        return 1
    print("Put this in .env as SENTRY_DSN:")
    print(dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

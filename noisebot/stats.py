"""Hourly issue-stat buckets from ``groupStatsPeriod=24h``."""

from __future__ import annotations


def sum_recent_buckets(stats: list | None, window_hours: int) -> int:
    """Sum the newest ``window_hours`` buckets.

    Sentry returns ``stats["24h"]`` as ``[unix_ts, count]`` pairs, oldest first,
    one pair per hour. Buckets are sorted by timestamp so a reversed payload
    still counts the latest hours.
    """

    if window_hours <= 0 or not stats:
        return 0
    points: list[tuple[int, int]] = []
    for item in stats:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            points.append((int(item[0]), int(item[1])))
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda pair: pair[0])
    return sum(count for _, count in points[-window_hours:])

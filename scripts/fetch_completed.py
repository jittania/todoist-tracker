#!/usr/bin/env python3
"""
Fetch Todoist completed tasks since last run and append to activity/completed.md.
Uses GET .../tasks/completed/by_completion_date with since/until and cursor pagination.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# API (3-month range limit)
BASE_URL = "https://api.todoist.com/api/v1/tasks/completed/by_completion_date"
THREE_MONTHS_DAYS = 90

# Paths (repo root = parent of scripts/)
REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "state.json"
LOG_PATH = REPO_ROOT / "activity" / "completed.md"

# ID in log for deduplication: hidden HTML comment
ID_COMMENT_RE = re.compile(r"<!-- id:(\d+) -->")


def get_state() -> dict:
    """Load state.json; return dict with last_run_iso and optionally logged_ids."""
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not read state: {e}", file=sys.stderr)
        return {}


def save_state(state: dict) -> None:
    """Write state.json."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def existing_logged_ids() -> set[str]:
    """Parse activity/completed.md for existing task ids (<!-- id:123 -->)."""
    ids: set[str] = set()
    if not LOG_PATH.exists():
        return ids
    try:
        text = LOG_PATH.read_text(encoding="utf-8")
        for m in ID_COMMENT_RE.finditer(text):
            ids.add(m.group(1))
    except OSError:
        pass
    return ids


def fetch_completed(
    token: str,
    since_iso: str,
    until_iso: str,
) -> list[dict]:
    """Fetch all completed tasks in [since_iso, until_iso] with pagination."""
    # Clamp to 3 months
    since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    until_dt = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
    if (until_dt - since_dt).days > THREE_MONTHS_DAYS:
        since_dt = until_dt - timedelta(days=THREE_MONTHS_DAYS)
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_items: list[dict] = []
    cursor: str | None = ""
    headers = {"Authorization": f"Bearer {token}"}

    while True:
        params: dict = {"since": since_iso, "until": until_iso}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(BASE_URL, params=params, headers=headers, timeout=30)
        if not resp.ok:
            print(f"HTTP error: {resp.status_code} {resp.reason}", file=sys.stderr)
            print(resp.text[:500], file=sys.stderr)
            sys.exit(1)

        data = resp.json()
        items = data.get("items") or []
        all_items.extend(items)
        cursor = data.get("next_cursor")
        if not cursor:
            break

    return all_items


def main() -> None:
    token = os.environ.get("TODOIST_API_TOKEN", "").strip()
    if not token:
        print("Error: TODOIST_API_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    state = get_state()
    now = datetime.now(timezone.utc)
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    last = state.get("last_run_iso")
    if last:
        start_iso = last
    else:
        start_dt = now - timedelta(hours=24)
        start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    logged_ids = existing_logged_ids()
    items = fetch_completed(token, start_iso, end_iso)

    new_lines: list[str] = []
    for item in items:
        task_id = str(item.get("id", ""))
        if task_id in logged_ids:
            continue
        content = item.get("content") or ""
        completed_at = item.get("completed_at") or ""
        date_part = completed_at[:10] if len(completed_at) >= 10 else completed_at
        if not date_part:
            continue
        # Escape newlines in content for single-line log
        content_safe = content.replace("\n", " ")
        line = f"- {date_part} — {content_safe} <!-- id:{task_id} -->\n"
        new_lines.append(line)
        logged_ids.add(task_id)

    if new_lines:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.writelines(new_lines)

    state["last_run_iso"] = end_iso
    save_state(state)


if __name__ == "__main__":
    main()

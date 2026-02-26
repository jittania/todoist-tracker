#!/usr/bin/env python3
"""
Fetch Todoist completed tasks since last run; maintain event store and render
activity/completed.md grouped by week (America/Los_Angeles, Monday start) with metadata.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# API (3-month range limit for completed)
API_BASE = "https://api.todoist.com/api/v1"
COMPLETED_URL = f"{API_BASE}/tasks/completed/by_completion_date"
PROJECTS_URL = f"{API_BASE}/projects"
TASK_URL_TEMPLATE = f"{API_BASE}/tasks/{{task_id}}"
THREE_MONTHS_DAYS = 90
WEEK_TZ = ZoneInfo("America/Los_Angeles")

# Paths (repo root = parent of scripts/)
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"
STATE_PATH = REPO_ROOT / "state.json"
ACTIVITY_DIR = REPO_ROOT / "activity"
EVENTS_PATH = ACTIVITY_DIR / "completed_events.jsonl"
LOG_PATH = ACTIVITY_DIR / "completed.md"
TASK_CACHE_PATH = ACTIVITY_DIR / "task_cache.json"


def load_config() -> dict:
    """Load config.json; return {} if missing or invalid."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get_allowed_root_ids(config: dict) -> set[str]:
    """Return set of allowed root task IDs (strings). Empty if missing or empty list."""
    raw = config.get("allowed_root_task_ids")
    if raw is None or not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def get_state() -> dict:
    """Load state.json; return dict with last_run_iso."""
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


def load_events() -> list[dict]:
    """Load all events from completed_events.jsonl (one JSON object per line)."""
    events: list[dict] = []
    if not EVENTS_PATH.exists():
        return events
    try:
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"Warning: could not read event store: {e}", file=sys.stderr)
    return events


def append_events(new_events: list[dict]) -> None:
    """Append new event dicts to completed_events.jsonl."""
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        for ev in new_events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def http_get(url: str, token: str, params: dict | None = None) -> requests.Response:
    """GET with Bearer token; raise on non-OK (caller can exit)."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    if not resp.ok:
        print(f"HTTP error: {resp.status_code} {resp.reason}", file=sys.stderr)
        print(resp.text[:500], file=sys.stderr)
        sys.exit(1)
    return resp


def fetch_projects(token: str) -> dict[str, str]:
    """Fetch all projects and return id -> name map."""
    id_to_name: dict[str, str] = {}
    cursor: str | None = ""
    while True:
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        resp = http_get(PROJECTS_URL, token, params if params else None)
        raw = resp.json()
        # API may return a list (REST v2) or dict with results/projects + next_cursor
        if isinstance(raw, list):
            projects_list = raw
            cursor = None
        else:
            projects_list = raw.get("projects") or raw.get("results") or []
            cursor = raw.get("next_cursor")
        for p in projects_list:
            pid = str(p.get("id", ""))
            name = (p.get("name") or "").strip() or "(No name)"
            id_to_name[pid] = name
        if not cursor:
            break
    return id_to_name


def fetch_task(token: str, task_id: str) -> dict | None:
    """Fetch a single task by id; return None on 404 or error."""
    url = TASK_URL_TEMPLATE.format(task_id=task_id)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404 or not resp.ok:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def load_task_cache() -> dict[str, dict]:
    """Load task_cache.json: task_id -> {content, parent_id, project_id}."""
    if not TASK_CACHE_PATH.exists():
        return {}
    try:
        with open(TASK_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): v for k, v in (data or {}).items() if isinstance(v, dict)}
    except (json.JSONDecodeError, OSError):
        return {}


def save_task_cache(cache: dict[str, dict]) -> None:
    """Write task_cache.json (lightweight: task_id -> content, parent_id, project_id)."""
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    with open(TASK_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
        f.write("\n")


def task_info_from_api(task: dict) -> dict:
    """Extract {content, parent_id, project_id} from API task dict."""
    parent_id = task.get("parent_id")
    return {
        "content": (task.get("content") or "").strip(),
        "parent_id": str(parent_id) if parent_id else "",
        "project_id": str(task.get("project_id", "")),
    }


def get_task_info(
    token: str,
    task_id: str,
    cache: dict[str, dict],
    cache_updates: dict[str, dict],
) -> dict | None:
    """Resolve task_id to {content, parent_id, project_id}. Use cache then API. Return None if unresolved."""
    if not task_id:
        return None
    if task_id in cache:
        return cache[task_id]
    if task_id in cache_updates:
        return cache_updates[task_id]
    task = fetch_task(token, task_id)
    if not task:
        return None
    info = task_info_from_api(task)
    cache_updates[task_id] = info
    return info


def is_task_allowed(
    task_id: str,
    parent_id: str,
    allowed_ids: set[str],
    token: str,
    cache: dict[str, dict],
    cache_updates: dict[str, dict],
) -> bool:
    """
    True if task_id is in allowed_ids or any ancestor is. Walk chain via parent_id; use cache + API.
    If chain cannot be fully resolved (missing parent), return False (fail closed).
    """
    if not task_id:
        return False
    if task_id in allowed_ids:
        return True
    current_id = task_id
    next_parent_id = (parent_id or "").strip()
    while next_parent_id:
        info = get_task_info(token, next_parent_id, cache, cache_updates)
        if info is None:
            return False
        if next_parent_id in allowed_ids:
            return True
        current_id = next_parent_id
        next_parent_id = (info.get("parent_id") or "").strip()
    return False


def fetch_completed(
    token: str,
    since_iso: str,
    until_iso: str,
) -> list[dict]:
    """Fetch all completed tasks in [since_iso, until_iso] with pagination."""
    since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    until_dt = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
    if (until_dt - since_dt).days > THREE_MONTHS_DAYS:
        since_dt = until_dt - timedelta(days=THREE_MONTHS_DAYS)
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_items: list[dict] = []
    cursor: str | None = ""
    while True:
        params: dict = {"since": since_iso, "until": until_iso}
        if cursor:
            params["cursor"] = cursor
        resp = http_get(COMPLETED_URL, token, params)
        data = resp.json()
        items = data.get("items") or []
        all_items.extend(items)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return all_items


def event_from_item(item: dict) -> dict:
    """Build event dict from API item (id, content, completed_at, project_id, parent_id, priority)."""
    return {
        "id": str(item.get("id", "")),
        "content": (item.get("content") or "").strip(),
        "completed_at": item.get("completed_at") or "",
        "project_id": str(item.get("project_id", "")),
        "parent_id": str(item.get("parent_id", "")) if item.get("parent_id") else "",
        "priority": int(item.get("priority", 1)) if item.get("priority") is not None else 1,
    }


def resolve_parent_title(
    parent_id: str,
    id_to_content: dict[str, str],
    task_cache: dict[str, dict] | None = None,
) -> str:
    """Return parent title from event store or task cache only (no API fetch for display, to avoid leaking)."""
    if not parent_id:
        return ""
    if parent_id in id_to_content:
        return id_to_content[parent_id]
    if task_cache and parent_id in task_cache:
        return (task_cache[parent_id].get("content") or "").strip() or "(No title)"
    return f"(parent_id: {parent_id})"


def completed_at_to_local_date(completed_at: str) -> tuple[datetime, str]:
    """Parse completed_at (ISO UTC) and return (local datetime, YYYY-MM-DD) in America/Los_Angeles."""
    if not completed_at:
        return datetime.min.replace(tzinfo=WEEK_TZ), "0000-00-00"
    try:
        dt_utc = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        local = dt_utc.astimezone(WEEK_TZ)
        return local, local.strftime("%Y-%m-%d")
    except Exception:
        return datetime.min.replace(tzinfo=WEEK_TZ), "0000-00-00"


def week_start_local(local_dt: datetime) -> datetime:
    """Monday of the week for local_dt (America/Los_Angeles)."""
    # weekday(): Monday=0, Sunday=6
    days_back = local_dt.weekday()
    monday = local_dt - timedelta(days=days_back)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def render_completed_md(
    events: list[dict],
    project_map: dict[str, str],
    task_cache: dict[str, dict] | None = None,
) -> str:
    """Generate completed.md content: grouped by week (Mon start, LA), chronological within week."""
    id_to_content = {e["id"]: e["content"] for e in events}

    # Enrich each event with local date, project name, parent suffix (no API fetch for parent title)
    enriched: list[dict] = []
    for e in events:
        local_dt, date_str = completed_at_to_local_date(e.get("completed_at") or "")
        project_name = project_map.get(e.get("project_id") or "", "Unknown")
        priority = e.get("priority", 1)
        content_safe = (e.get("content") or "").replace("\n", " ")
        parent_id = (e.get("parent_id") or "").strip()
        if parent_id:
            parent_title = resolve_parent_title(parent_id, id_to_content, task_cache)
            if parent_title and not parent_title.startswith("(parent_id:"):
                parent_suffix = f" (parent: {parent_title})"
            else:
                parent_suffix = f" (parent_id: {parent_id})"
        else:
            parent_suffix = ""
        enriched.append({
            "local_dt": local_dt,
            "date_str": date_str,
            "project_name": project_name,
            "priority": priority,
            "content_safe": content_safe,
            "parent_suffix": parent_suffix,
        })

    # Sort by completed_at local time
    enriched.sort(key=lambda x: x["local_dt"])

    # Group by week (Monday date as key)
    by_week: dict[datetime, list[dict]] = defaultdict(list)
    for ev in enriched:
        week_monday = week_start_local(ev["local_dt"])
        by_week[week_monday].append(ev)

    # Output: ## Week of YYYY-MM-DD then lines
    lines: list[str] = [
        "# Completed tasks",
        "",
        "Grouped by week (America/Los_Angeles, Monday start).",
        "",
    ]
    for week_monday in sorted(by_week.keys()):
        heading_date = week_monday.strftime("%Y-%m-%d")
        lines.append(f"## Week of {heading_date}")
        lines.append("")
        for ev in by_week[week_monday]:
            line = f"- {ev['date_str']} — [{ev['project_name']}] (P{ev['priority']}) {ev['content_safe']}{ev['parent_suffix']}"
            lines.append(line)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    token = os.environ.get("TODOIST_API_TOKEN", "").strip()
    if not token:
        print("Error: TODOIST_API_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    allowed_ids = get_allowed_root_ids(config)
    if not allowed_ids:
        # Safety: no allowlist -> write nothing, exit successfully
        sys.exit(0)

    state = get_state()
    now = datetime.now(timezone.utc)
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    last = state.get("last_run_iso")
    if last:
        start_iso = last
    else:
        start_dt = now - timedelta(hours=24)
        start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    cache = load_task_cache()
    cache_updates: dict[str, dict] = {}

    # Fetch projects once per run
    project_map = fetch_projects(token)
    items = fetch_completed(token, start_iso, end_iso)

    events = load_events()
    existing_ids = {e.get("id") for e in events if e.get("id")}
    new_events: list[dict] = []
    for item in items:
        eid = str(item.get("id", ""))
        if not eid or eid in existing_ids:
            continue
        parent_id = str(item.get("parent_id") or "")
        if not is_task_allowed(eid, parent_id, allowed_ids, token, cache, cache_updates):
            continue
        new_events.append(event_from_item(item))
        existing_ids.add(eid)

    if new_events:
        append_events(new_events)
        events = events + new_events

    # Only render tasks that are under allowed roots (filter in case allowlist changed)
    full_cache = {**cache, **cache_updates}
    allowed_events = [
        e for e in events
        if is_task_allowed(
            e.get("id") or "",
            (e.get("parent_id") or "").strip(),
            allowed_ids,
            token,
            cache,
            cache_updates,
        )
    ]

    md_content = render_completed_md(allowed_events, project_map, full_cache)
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(md_content, encoding="utf-8")

    save_task_cache(full_cache)
    state["last_run_iso"] = end_iso
    save_state(state)


if __name__ == "__main__":
    main()

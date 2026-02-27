#!/usr/bin/env python3
"""
Fetch Todoist completed tasks since last run; maintain event store and render
activity/completed.md grouped by week (America/Los_Angeles, Monday start) with metadata.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# API (3-month range limit for completed)
API_BASE = "https://api.todoist.com/api/v1"
COMPLETED_URL = f"{API_BASE}/tasks/completed/by_completion_date"
PROJECTS_URL_V1 = f"{API_BASE}/projects"
PROJECTS_URL_V2 = "https://api.todoist.com/rest/v2/projects"
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
    result = set()
    for x in raw:
        s = str(x).strip()
        if s:
            result.add(s)
    return result


def get_allowed_root_ids_from_env_or_config() -> set[str]:
    """
    Return allowed root task IDs: from env TODOIST_ALLOWED_ROOT_TASK_IDS (comma-separated)
    first, else from config.json. Use env/secret in CI so config.json can stay private.
    """
    env_val = os.environ.get("TODOIST_ALLOWED_ROOT_TASK_IDS", "").strip()
    if env_val:
        result = set()
        for part in env_val.split(","):
            s = part.strip()
            if s:
                result.add(s)
        return result
    config = load_config()
    return get_allowed_root_ids(config)


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
    """Fetch all projects and return id -> name map. Tries api/v1 first, then rest/v2 on 404."""
    id_to_name: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {token}"}

    def parse_response(raw: requests.Response) -> tuple[list, str | None]:
        data = raw.json()
        if isinstance(data, list):
            return data, None
        projects_list = data.get("projects") or data.get("results") or []
        cursor = data.get("next_cursor")
        return projects_list, cursor

    # Try api/v1 first (supports cursor pagination)
    cursor: str | None = ""
    try:
        while True:
            params = {"cursor": cursor} if cursor else None
            resp = requests.get(PROJECTS_URL_V1, params=params, headers=headers, timeout=30)
            if resp.status_code == 404 or resp.status_code == 401:
                break
            if not resp.ok:
                print(f"HTTP error: {resp.status_code} {resp.reason}", file=sys.stderr)
                print(resp.text[:500], file=sys.stderr)
                sys.exit(1)
            projects_list, cursor = parse_response(resp)
            for p in projects_list:
                pid = str(p.get("id", ""))
                name_raw = (p.get("name") or "").strip()
                id_to_name[pid] = name_raw or "(No name)"
            if not cursor:
                return id_to_name
    except Exception:
        pass

    # Fallback: rest/v2 (returns a flat list)
    try:
        resp = requests.get(PROJECTS_URL_V2, headers=headers, timeout=30)
        if not resp.ok:
            return id_to_name
        projects_list, _ = parse_response(resp)
        for p in projects_list:
            pid = str(p.get("id", ""))
            name_raw = (p.get("name") or "").strip()
            id_to_name[pid] = name_raw or "(No name)"
    except Exception:
        pass
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
    except (json.JSONDecodeError, OSError):
        return {}
    result = {}
    for k, v in (data or {}).items():
        if isinstance(v, dict):
            result[str(k)] = v
    return result


def save_task_cache(cache: dict[str, dict]) -> None:
    """Write task_cache.json (lightweight: task_id -> content, parent_id, project_id)."""
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    with open(TASK_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
        f.write("\n")


def task_info_from_api(task: dict) -> dict:
    """Extract {content, parent_id, project_id} from API task dict."""
    content_raw = task.get("content") or ""
    content = content_raw.strip()

    parent_id_raw = task.get("parent_id")
    if parent_id_raw:
        parent_id = str(parent_id_raw)
    else:
        parent_id = ""

    project_id = str(task.get("project_id", ""))

    return {
        "content": content,
        "parent_id": parent_id,
        "project_id": project_id,
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
    """Build event dict from API item (id, content, completed_at, project_id, parent_id, priority).
    Todoist sends inverted priority (1 = lowest, 4 = highest); we store emoji for display.
    """
    raw_priority = item.get("priority")
    if raw_priority is not None:
        try:
            p = int(raw_priority)
        except (TypeError, ValueError):
            p = None
    else:
        p = None
    if p == 1:
        priority = "⚪️"
    elif p == 2:
        priority = "🔵"
    elif p == 3:
        priority = "🟠"
    elif p == 4:
        priority = "🔴"
    else:
        priority = "❌"

    raw_parent_id = item.get("parent_id")
    if raw_parent_id:
        parent_id = str(raw_parent_id)
    else:
        parent_id = ""

    content_raw = item.get("content") or ""
    content = content_raw.strip()

    return {
        "id": str(item.get("id", "")),
        "content": content,
        "completed_at": item.get("completed_at") or "",
        "project_id": str(item.get("project_id", "")),
        "parent_id": parent_id,
        "priority": priority,
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
        cached = task_cache[parent_id]
        content_raw = cached.get("content") or ""
        content = content_raw.strip()
        if content:
            return content
        return "(No title)"
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


def _day_ordinal(day: int) -> str:
    """Return ordinal suffix for day of month: 1 -> 'st', 2 -> 'nd', 26 -> 'th'."""
    if 11 <= day <= 13:
        return "th"
    if day % 10 == 1:
        return "st"
    if day % 10 == 2:
        return "nd"
    if day % 10 == 3:
        return "rd"
    return "th"


def _format_entry_date(local_dt: datetime) -> str:
    """Format date for entry heading: 'Thursday, Feb 26th'."""
    weekday = local_dt.strftime("%A")
    month_abbr = local_dt.strftime("%b")
    day = local_dt.day
    ordinal = _day_ordinal(day)
    return f"{weekday}, {month_abbr} {day}{ordinal}"


# Append-only: initial file header (no week heading; that is added when first appending)
COMPLETED_MD_HEADER = """# Completed tasks

Grouped by week (America/Los_Angeles, Monday start). Append-only.

"""


def _enrich_event_for_display(
    e: dict,
    id_to_content: dict[str, str],
    project_map: dict[str, str],
    task_cache: dict[str, dict] | None,
) -> tuple[datetime, str, str, str, str, str]:
    """Return (local_dt, date_line, priority_display, content_safe, parent_display, project_name) for one event."""
    completed_at = e.get("completed_at") or ""
    local_dt, _ = completed_at_to_local_date(completed_at)

    project_id = e.get("project_id") or ""
    project_name = project_map.get(project_id, "Unknown")

    # Priority: event may store emoji (new) or raw int (legacy); show emoji either way (Todoist inverted 1–4)
    p = e.get("priority", 1)
    if isinstance(p, str):
        priority_display = p
    elif p == 1:
        priority_display = "⚪️"
    elif p == 2:
        priority_display = "🔵"
    elif p == 3:
        priority_display = "🟠"
    elif p == 4:
        priority_display = "🔴"
    else:
        priority_display = "❌"

    content_raw = e.get("content") or ""
    content_safe = content_raw.replace("\n", " ")

    parent_id_raw = e.get("parent_id") or ""
    parent_id = parent_id_raw.strip()
    if parent_id:
        parent_title = resolve_parent_title(parent_id, id_to_content, task_cache)
        if parent_title and not parent_title.startswith("(parent_id:"):
            parent_display = parent_title
        else:
            parent_display = f"(parent_id: {parent_id})"
    else:
        parent_display = "—"

    date_line = _format_entry_date(local_dt)
    return local_dt, date_line, priority_display, content_safe, parent_display, project_name


def _build_grouped_blocks(
    enriched: list[tuple[datetime, str, str, str, str, str]],
) -> str:
    """Group enriched events by date then by (Goal, Project); output one block per date with Goal subsections."""
    if not enriched:
        return ""
    # Sort by local_dt
    sorted_enriched = sorted(enriched, key=lambda x: x[0])
    # date_line -> list of (parent_display, project_name, [(priority_display, content_safe), ...]) in order
    date_order = []
    groups_by_date = {}

    for row in sorted_enriched:
        local_dt, date_line, priority_display, content_safe, parent_display, project_name = row
        date_line = date_line or "Unknown date"
        priority_display = priority_display if priority_display is not None else "❌"
        content_safe = content_safe if content_safe is not None else ""
        parent_display = parent_display if parent_display is not None else "—"
        project_name = project_name if project_name is not None else "Unknown"
        if date_line not in groups_by_date:
            groups_by_date[date_line] = []
            date_order.append(date_line)
        grp_list = groups_by_date[date_line]
        found = False
        for i in range(len(grp_list)):
            p, proj, tasks = grp_list[i]
            if (p, proj) == (parent_display, project_name):
                tasks.append((priority_display, content_safe))
                found = True
                break
        if not found:
            grp_list.append((parent_display, project_name, [(priority_display, content_safe)]))

    blocks = []
    for date_line in date_order:
        lines = [f"- **{date_line}**"]
        for parent_display, project_name, tasks in groups_by_date[date_line]:
            lines.append(f"  - Goal: `**{parent_display}**` | {project_name}")
            for pri, content in tasks:
                lines.append(f"    - {pri} {content}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def append_to_completed_md(
    new_events: list[dict],
    project_map: dict[str, str],
    task_cache: dict[str, dict] | None,
) -> None:
    """
    Append only new task lines to completed.md. Never overwrite or re-render.
    If current week heading is not the last in the file, append it first; then append lines.
    """
    if not new_events:
        return
    id_to_content = {}
    for e in new_events:
        id_to_content[e["id"]] = e["content"]
    enriched = []
    for e in new_events:
        row = _enrich_event_for_display(e, id_to_content, project_map, task_cache)
        enriched.append(row)
    lines_str = _build_grouped_blocks(enriched)

    now_la = datetime.now(WEEK_TZ)
    week_monday = week_start_local(now_la)
    heading_date = week_monday.strftime("%Y-%m-%d")
    week_heading_re = re.compile(r"^## Week of (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)

    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)

    if not LOG_PATH.exists() or LOG_PATH.read_text(encoding="utf-8").strip() == "":
        LOG_PATH.write_text(
            COMPLETED_MD_HEADER + f"## Week of {heading_date}\n\n" + lines_str + "\n",
            encoding="utf-8",
        )
        return

    content = LOG_PATH.read_text(encoding="utf-8")
    matches = week_heading_re.findall(content)
    if matches:
        last_week_date = matches[-1]
    else:
        last_week_date = None

    current_week_is_last_heading = (last_week_date == heading_date)
    if current_week_is_last_heading:
        to_append = "\n" + lines_str + "\n"
    else:
        to_append = "\n\n## Week of " + heading_date + "\n\n" + lines_str + "\n"

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(to_append)


def main() -> None:
    token = os.environ.get("TODOIST_API_TOKEN", "").strip()
    if not token:
        print("Error: TODOIST_API_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    allowed_ids = get_allowed_root_ids_from_env_or_config()
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
    existing_ids = set()
    for e in events:
        eid = e.get("id")
        if eid:
            existing_ids.add(eid)
    new_events = []
    for item in items:
        eid = str(item.get("id", ""))
        if not eid or eid in existing_ids:
            continue
        parent_id = str(item.get("parent_id") or "")
        if not is_task_allowed(eid, parent_id, allowed_ids, token, cache, cache_updates):
            continue
        new_events.append(event_from_item(item))
        existing_ids.add(eid)

    full_cache = {**cache, **cache_updates}

    if new_events:
        append_events(new_events)
        append_to_completed_md(new_events, project_map, full_cache)

    save_task_cache(full_cache)
    state["last_run_iso"] = end_iso
    save_state(state)


if __name__ == "__main__":
    main()

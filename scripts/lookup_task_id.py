#!/usr/bin/env python3
"""
Search active Todoist tasks by text query and print matching tasks with id, content, and project.
Use this to find task IDs for config.json allowed_root_task_ids.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

# REST v2 returns a flat list of tasks
API_BASE = "https://api.todoist.com/rest/v2"
TASKS_URL = f"{API_BASE}/tasks"
PROJECTS_URL = f"{API_BASE}/projects"


def main() -> None:
    token = os.environ.get("TODOIST_API_TOKEN", "").strip()
    if not token:
        print("Error: TODOIST_API_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:]).strip()
    else:
        query = ""
    if not query:
        print("Usage: python scripts/lookup_task_id.py <search text>", file=sys.stderr)
        print("Example: python scripts/lookup_task_id.py 'Land a salaried job'", file=sys.stderr)
        sys.exit(1)

    headers = {"Authorization": f"Bearer {token}"}

    # Fetch projects for name lookup
    projects: dict[str, str] = {}
    try:
        r = requests.get(PROJECTS_URL, headers=headers, timeout=30)
        if r.ok:
            for p in r.json() or []:
                pid = str(p.get("id", ""))
                name_raw = (p.get("name") or "").strip()
                if name_raw:
                    projects[pid] = name_raw
                else:
                    projects[pid] = "(No name)"
    except Exception:
        pass

    # Fetch active tasks (REST v2 returns list; no cursor in v2 tasks list)
    try:
        r = requests.get(TASKS_URL, headers=headers, timeout=30)
        if not r.ok:
            print(f"HTTP error: {r.status_code} {r.reason}", file=sys.stderr)
            sys.exit(1)
        tasks = r.json() or []
    except Exception as e:
        print(f"Error fetching tasks: {e}", file=sys.stderr)
        sys.exit(1)

    query_lower = query.lower()
    matches = []
    for t in tasks:
        content = t.get("content") or ""
        if query_lower in content.lower():
            matches.append(t)

    if not matches:
        print(f"No active tasks matching '{query}'.")
        return

    print(f"Tasks matching '{query}' (copy id into config.json allowed_root_task_ids):")
    print()
    for t in matches:
        tid = t.get("id", "")
        content_raw = t.get("content") or ""
        content = content_raw.strip()
        pid = str(t.get("project_id", ""))
        if pid in projects:
            project_name = projects[pid]
        else:
            project_name = pid if pid else "—"
        print(f"  id: {tid}")
        print(f"  content: {content}")
        print(f"  project: {project_name}")
        print()

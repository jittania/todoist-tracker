# Todoist Tracker
---
## **Cursor Prompts Used**

### **Cursor Prompt 1**

```
Build a repo called todoist-tracker.

Goal:
- On a schedule (GitHub Actions), fetch Todoist tasks completed since the last run, and append them to a markdown log.
- For now only record: completed date (YYYY-MM-DD) and task content.

Todoist API:
- Use GET https://api.todoist.com/api/v1/tasks/completed/by_completion_date with required query params since and until.
- Handle pagination: response includes next_cursor; pass it back as cursor until exhausted.
- Note: endpoint range is limited to 3 months; keep requests within that window.

Repo structure:
- scripts/fetch_completed.py
- activity/completed.md (append-only log)
- state.json (stores last_run_iso, e.g. "2026-02-26T20:15:00Z")
- .github/workflows/todoist-tracker.yml
- requirements.txt

Behavior:
- Read TODOIST_API_TOKEN from env.
- Determine window:
  - start = state.last_run_iso (or now-24h if missing)
  - end = now (UTC)
- Fetch all completed items in [start, end].
- For each item, write a line: "- {completed_at[:10]} — {content}".
- Deduplicate by Todoist item id so reruns don’t double-log.
- Update state.json last_run_iso = end after successful write.

GitHub Actions:
- Run hourly and on workflow_dispatch.
- Commit and push updated activity/completed.md and state.json using GITHUB_TOKEN.

Include basic error handling and exit non-zero on HTTP errors.
```

### **Cursor Prompt 2**

```
Update todoist-tracker to improve the log format + include more metadata.

New requirements
1) Group by week
- Output file: activity/completed.md
- Group entries under headings by week (America/Los_Angeles), with Monday as the start of week.
- Heading format: "## Week of YYYY-MM-DD" (YYYY-MM-DD is that Monday date)
- Under each week, list tasks in chronological order (by completed_at local time).

2) Include metadata per completed task
For each completed item, include:
- completed_date (local YYYY-MM-DD)
- task content (title)
- priority (1–4) as returned by the API at completion time
- project name (resolve from project_id)
- parent task title if it has a parent_id (resolve if possible), otherwise show parent_id.

Data sources (Todoist API v1)
- Completed tasks: GET https://api.todoist.com/api/v1/tasks/completed/by_completion_date (already used)
  - Use fields in returned items: id, content, completed_at, project_id, parent_id, priority
- Projects for name mapping:
  - Fetch projects once per run and build id->name map (use the Todoist API v1 projects endpoint).
- Parent title resolution:
  - Best-effort:
    - If parent_id exists and the parent task is also present in our local completed-items history cache, use its content as the parent title.
    - Else, try fetching parent via task endpoint if available; if that fails, fall back to displaying "(parent_id: <id>)".

3) Make weekly grouping easy to maintain
- Stop treating completed.md as append-only.
- Add an event store file: activity/completed_events.jsonl (one JSON object per completed item).
- On each run:
  - Fetch completed items for [since, until]
  - Deduplicate by completed item id against the event store
  - Append new unique events to completed_events.jsonl
  - Re-render activity/completed.md from the full event store every run (grouped by week as specified)
- Keep state.json for last_run_iso (same behavior as now).

Formatting spec (per task line)
- "- YYYY-MM-DD — [Project Name] (P{priority}) Task content"
- If parent exists and resolved:
  - add " (parent: Parent Title)"
- If parent exists and not resolved:
  - add " (parent_id: <id>)"

Implementation notes
- Convert completed_at to America/Los_Angeles for dates + weekly grouping.
- Keep API calls minimal; cache project map per run.
- Keep error handling: fail run on HTTP errors; do not update state.json if render/write fails.
- Update .gitignore only if needed (do NOT ignore completed.md or event store).

Deliverables
- Update scripts/fetch_completed.py accordingly
- Add activity/completed_events.jsonl handling
```

---
## **To Do:**

- [X] Add more metadata (priority level, parent task, parent project)
- [X] Group by week
- [ ] Only publish activity for tasks that belong to specific parent tasks
- [ ] Make repo public
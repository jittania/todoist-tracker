# Todoist Tracker

Fetches Todoist completed tasks on a schedule and publishes only those under an allowlist of root task IDs to `activity/completed.md` (grouped by week, with metadata).

---

## Configuration

- **`config.json`** (repo root) must contain an allowlist of Todoist **task IDs** (not names). Only completed tasks that are the root itself or a descendant (child/subtask at any depth) of one of these roots are included in the log.

  Example:
  ```json
  {
    "allowed_root_task_ids": [1234567890, 9876543210]
  }
  ```
  Use integer IDs. Filtering uses only IDs so renaming a root task won’t break anything.

- If `config.json` is missing or `allowed_root_task_ids` is empty, the script writes nothing and exits successfully (no data is published).

## Finding task IDs

To get the ID of a root task (e.g. to add to `allowed_root_task_ids`):

1. Set `TODOIST_API_TOKEN` in your environment (same token as for the tracker).
2. Run the lookup script with a search phrase that appears in the task’s title:
   ```bash
   python scripts/lookup_task_id.py "Land a salaried job"
   ```
3. The script prints matching **active** tasks with `id`, `content`, and `project`. Copy the `id` value(s) into `config.json` → `allowed_root_task_ids`.

Only active tasks are searchable. If your root is already completed, look up the ID in the Todoist app (e.g. task URL or API) or add the ID to config before completing it.

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

### **Cursor Prompt 3**

```
Update todoist-tracker to ONLY publish completed tasks that roll up under an allowlist of specific “root” parent tasks, using stable task IDs (not names), so renaming the parent task won’t break filtering.

Goal
- Repo will be public.
- Only include completed tasks that are descendants (children/subtasks at any depth) of these two root parent tasks:
  1) "AI developer rebrand: get comfortable with AI dev and productivity tools, do a small project for portfolio, contribute to an open-source AI project"
  2) "Land a salaried job by end of 2026"

Key requirement: use IDs, not names
- Add a config file at repo root: config.json
- It must contain an allowlist of Todoist task IDs, e.g.
  {
    "allowed_root_task_ids": [1234567890, 9876543210]
  }
- Filtering logic must only rely on IDs. Names are only for display.

How to determine if a completed task is allowed
- For each completed item, determine its ancestor chain via parent_id links.
- Include the completed task if:
  - its own id is in allowed_root_task_ids, OR
  - any ancestor task id up the chain is in allowed_root_task_ids.
- If the ancestor chain cannot be fully resolved (missing parent info), default to EXCLUDE (fail closed), since the repo is public.

Data + API behavior
- Completed items come from GET https://api.todoist.com/api/v1/tasks/completed/by_completion_date (already used).
- Each completed item may include parent_id.
- Implement a parent-resolution helper that can fetch task details by id when needed to walk up the chain.
- Cache fetched task details in-memory per run to reduce API calls.
- Optionally persist a lightweight cache file (activity/task_cache.json) mapping task_id -> {content, parent_id, project_id} to reduce future calls.

UI/Docs
- Update README.md with:
  - How to set allowed_root_task_ids
  - How to find a task ID:
    - Provide a small helper script scripts/lookup_task_id.py that searches active tasks by text query and prints matching tasks with ids (so the user can paste the IDs into config.json).
- Ensure logs only include allowed tasks and never leak other task titles.

Implementation deliverables
- Add config.json support
- Update scripts/fetch_completed.py to apply allowlist filtering before writing events/log
- Add scripts/lookup_task_id.py (search by query, list task ids + titles + project)
- Keep existing weekly grouping + metadata logging behavior from prior prompt

Safety
- If config.json is missing or allowed_root_task_ids is empty, write nothing and exit successfully (no accidental leak).
```

### **Cursor Prompt 4**

```
Additional objective: make completed.md append-only

Currently, the script re-renders and fully overwrites activity/completed.md on each successful run.

Change this behavior:

- Do NOT overwrite activity/completed.md.
- The file must be append-only.
- Only append newly discovered, allowed completed tasks.
- Never re-render historical weeks.
- Never delete or modify existing content.
- Deduplication must still prevent duplicate entries from being appended.

Weekly grouping behavior with append-only mode:
- If the current week heading (## Week of YYYY-MM-DD) does not exist at the bottom of the file, append a new heading.
- If it already exists as the most recent heading, append new task lines under it.
- Do not attempt to regroup or reorder older entries.

Important:
- Keep completed_events.jsonl for deduplication and state tracking.
- Filtering (allowed_root_task_ids) must still occur BEFORE appending.
- If no new eligible tasks are found, do nothing and exit cleanly.

The file should grow indefinitely until manually deleted.
```

---
## **To Do:**

- [X] Add more metadata (priority level, parent task, parent project)
- [X] Group by week
- [X] Only publish activity for tasks that belong to specific parent tasks (allowlist in config.json)
- [ ] Make log append-only (don't allow overwriting)
- [ ] Make repo public
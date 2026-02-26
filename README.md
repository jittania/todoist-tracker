# Todoist Tracker
---
## **Cursor Prompts**

### **Cursor Prompt 1**

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

---
## **To Do:**

- [ ] Group by week
- [ ] Add more metadata (priority level, parent task, parent project)
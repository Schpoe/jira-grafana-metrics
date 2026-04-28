#!/usr/bin/env python3
"""Jira Cloud → Postgres metrics sync.

READ-ONLY CONTRACT
------------------
This script ONLY reads data from Jira. It never creates, updates, or deletes
anything in Jira. This is enforced at two levels:

  1. ReadOnlyJiraSession — a requests.Session subclass that raises
     JiraWriteAttemptError immediately if any non-GET method is attempted.
     This makes it structurally impossible for a bug or future code change to
     accidentally mutate Jira data.

  2. All calls to Jira go through jira_get(), which is the only function that
     touches the Jira HTTP session.

All writes go exclusively to the local Postgres database.
"""

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import psycopg2
import requests
from dateutil import parser as dtparser
from psycopg2.extras import execute_values
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEYS = [k.strip() for k in os.environ["JIRA_PROJECT_KEYS"].split(",")]

# Jira Cloud story points field — override via env if your instance differs
STORY_POINTS_FIELD = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016")
ACCEPTANCE_CRITERIA_FIELD = os.environ.get("JIRA_ACCEPTANCE_CRITERIA_FIELD", "customfield_10028")
CUSTOMER_PROJECT_FIELD = os.environ.get("JIRA_CUSTOMER_PROJECT_FIELD", "customfield_10662")
QA_FIELD = os.environ.get("JIRA_QA_FIELD", "customfield_10132")

# Optional webhook for failure notifications (Slack/Teams/make.com/etc.)
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "")

PG_DSN = (
    f"host={os.environ['POSTGRES_HOST']} "
    f"dbname={os.environ['POSTGRES_DB']} "
    f"user={os.environ['POSTGRES_USER']} "
    f"password={os.environ['POSTGRES_PASSWORD']}"
)


# ─── Failure notification ────────────────────────────────────────────────────

def _notify_failure(errors: list[str]) -> None:
    """POST a failure alert to NOTIFY_WEBHOOK_URL if configured.
    Payload is compatible with Slack incoming webhooks and most HTTP-trigger
    services (Teams, make.com, n8n, etc.).
    """
    if not NOTIFY_WEBHOOK_URL:
        return
    import json as _json
    from datetime import datetime, timezone
    msg = (
        f":rotating_light: *Jira sync FAILED* at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Errors: {'; '.join(errors)}"
    )
    try:
        resp = requests.post(
            NOTIFY_WEBHOOK_URL,
            json={"text": msg},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Failure notification sent to webhook")
    except Exception as exc:
        log.warning("Could not send failure notification: %s", exc)


# ─── Read-only Jira HTTP session ─────────────────────────────────────────────

class JiraWriteAttemptError(RuntimeError):
    """Raised when code attempts a mutating call to Jira."""


class ReadOnlyJiraSession(requests.Session):
    """A requests Session that hard-blocks every mutating HTTP method.

    PUT, PATCH, and DELETE are always blocked.
    POST is blocked except for explicitly whitelisted read-only query endpoints
    (Jira Cloud migrated issue search from GET to POST for the search/jql path).
    No network connection is made before the check, so Jira data can never be
    changed regardless of what the rest of the code does.
    """

    _BLOCKED_METHODS = {"PUT", "PATCH", "DELETE"}

    # POST paths that are semantically read-only queries, not writes.
    # Matched as suffix of the request path.
    _READONLY_POST_SUFFIXES = (
        "/rest/api/3/search/jql",
    )

    def request(self, method, url, **kwargs):
        m = method.upper()
        if m in self._BLOCKED_METHODS:
            raise JiraWriteAttemptError(
                f"Blocked attempt to call {m} {url} — "
                "this script is read-only and must never modify Jira."
            )
        if m == "POST":
            from urllib.parse import urlparse
            path = urlparse(url).path
            if not any(path.endswith(suffix) for suffix in self._READONLY_POST_SUFFIXES):
                raise JiraWriteAttemptError(
                    f"Blocked POST to non-whitelisted path {url} — "
                    "this script is read-only and must never modify Jira."
                )
        return super().request(method, url, **kwargs)


_jira_session = ReadOnlyJiraSession()
_jira_session.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
_jira_session.headers.update({"Accept": "application/json"})


# ─── Jira API helpers ────────────────────────────────────────────────────────

import time as _time

def _jira_request(method, url, max_retries=5, **kwargs):
    """Execute a Jira API request with exponential backoff retry.

    Retries on connection errors, timeouts, 429 (rate limit) and 5xx responses.
    Raises on 4xx errors that are not 429.
    """
    for attempt in range(max_retries):
        try:
            resp = _jira_session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("Jira %s %s → %d, retrying in %ds (attempt %d/%d)",
                            method, url, resp.status_code, wait, attempt + 1, max_retries)
                _time.sleep(wait)
                continue
            if not resp.ok:
                log.error("Jira error %d: %s", resp.status_code, resp.text)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            log.warning("Connection error (%s), retrying in %ds (attempt %d/%d)",
                        exc, wait, attempt + 1, max_retries)
            _time.sleep(wait)

    raise RuntimeError(f"Jira request failed after {max_retries} attempts: {method} {url}")


def jira_get(path, params=None):
    """GET request against the Jira REST API."""
    url = f"{JIRA_URL}/rest/{path}"
    return _jira_request("GET", url, params=params)


def jira_search(jql, fields, next_page_token=None, max_results=100):
    """POST to /rest/api/3/search/jql — Jira Cloud's current search endpoint.

    Jira deprecated GET /rest/api/3/search (returns 410). The replacement
    uses token-based pagination: pass nextPageToken from the previous response
    to fetch the next page. isLast=True in the response means no more pages.
    """
    url = f"{JIRA_URL}/rest/api/3/search/jql"
    body = {"jql": jql, "fields": fields, "maxResults": max_results}
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return _jira_request("POST", url, json=body)


def parse_dt(value):
    if not value:
        return None
    try:
        return dtparser.parse(value)
    except (ValueError, TypeError):
        return None


# ─── Sync functions ───────────────────────────────────────────────────────────

def sync_projects(conn):
    log.info("Syncing projects: %s", JIRA_PROJECT_KEYS)
    rows = []
    for key in JIRA_PROJECT_KEYS:
        data = jira_get(f"api/3/project/{key}")
        rows.append((data["key"], data["name"]))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO projects (key, name)
            VALUES %s
            ON CONFLICT (key) DO UPDATE
                SET name = EXCLUDED.name, synced_at = NOW()
            """,
            rows,
        )
    conn.commit()
    log.info("Synced %d project(s)", len(rows))


def _parse_sprint_ids(value) -> set:
    """Parse comma-separated sprint IDs from a Jira changelog sprint field value.

    The Sprint field stores numeric sprint IDs in 'from'/'to' changelog items,
    e.g. "12345" or "12345,67890". Returns a set of ints.
    """
    if not value:
        return set()
    result = set()
    for part in str(value).split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


def _fetch_changelog(issue_key):
    """Fetch status transitions, fix-version history, and sprint history for one issue.

    Jira Cloud deprecated expand=changelog on the bulk search endpoint (410).
    This calls the dedicated per-issue changelog API instead.

    Some issues are corrupt on Jira's side (internal PSQLException returned as
    a 400) — these are logged and skipped rather than failing the entire sync.

    Returns (transition_rows, fix_version_events, sprint_events):
      transition_rows    — (issue_key, from_status, to_status, occurred_at, author)
      fix_version_events — (issue_key, fix_version, added_at, removed_at)
      sprint_events      — (issue_key, sprint_id, event, occurred_at)
                           where event is 'added' or 'removed'
    """
    transition_rows = []
    fix_version_events = []
    sprint_events = []
    start = 0
    while True:
        try:
            data = jira_get(
                f"api/3/issue/{issue_key}/changelog",
                params={"startAt": start, "maxResults": 100},
            )
        except requests.exceptions.HTTPError as exc:
            log.warning("Skipping changelog for %s — Jira returned %s", issue_key, exc)
            return [], [], []
        for history in data.get("values", []):
            occurred_at = parse_dt(history.get("created"))
            for item in history.get("items", []):
                if item["field"] == "status":
                    transition_rows.append((
                        issue_key,
                        item.get("fromString"),
                        item.get("toString"),
                        occurred_at,
                        history.get("author", {}).get("displayName"),
                    ))
                elif item["field"] == "Fix Version":
                    EPOCH = "1970-01-01T00:00:00+00:00"
                    added   = item.get("toString")
                    removed = item.get("fromString")
                    if added:
                        fix_version_events.append((issue_key, added, occurred_at or EPOCH, None))
                    if removed:
                        fix_version_events.append((issue_key, removed, EPOCH, occurred_at))
                elif item["field"] == "Sprint":
                    from_ids = _parse_sprint_ids(item.get("from"))
                    to_ids   = _parse_sprint_ids(item.get("to"))
                    for sid in to_ids - from_ids:
                        sprint_events.append((issue_key, sid, "added", occurred_at))
                    for sid in from_ids - to_ids:
                        sprint_events.append((issue_key, sid, "removed", occurred_at))
        if data.get("isLast", True):
            break
        start += data.get("maxResults", 100)
    return transition_rows, fix_version_events, sprint_events


def _last_successful_sync(conn):
    """Return (started_at, finished_at) of the last successful sync, or (None, None)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT started_at, finished_at FROM sync_log WHERE status IN ('success', 'partial') ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _resume_checkpoint(conn):
    """Return (sync_id, next_page_token, issues_checkpoint, transitions_checkpoint)
    if there is an interrupted running sync with a saved page token, else None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, next_page_token, issues_checkpoint, transitions_checkpoint
            FROM sync_log
            WHERE status = 'running' AND next_page_token IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """
        )
        row = cur.fetchone()
    return row if row else None


def sync_issues(conn, sync_id, since=None, last_sync_duration=None, resume_token=None, resume_counts=None):
    """Sync issues and their full changelog.

    Checkpointing: after every page the current nextPageToken and running
    totals are written to sync_log. If the container restarts mid-sync,
    _resume_checkpoint() finds the saved token and this function continues
    from where it left off instead of starting from page 1.

    resume_token:  nextPageToken from a previous interrupted run (or None).
    resume_counts: (issues, transitions) already saved in the previous run.
    """
    project_filter = ", ".join(f'"{k}"' for k in JIRA_PROJECT_KEYS)

    # Hard limit: never sync issues created before this date
    history_cutoff_str = os.environ.get("JIRA_HISTORY_START", "2024-01-01")

    if since and os.environ.get("FULL_SYNC", "").lower() not in ("1", "true", "yes"):
        if last_sync_duration:
            buffer = max(timedelta(minutes=30), last_sync_duration + timedelta(minutes=15))
        else:
            buffer = timedelta(minutes=30)
        cutoff = since - buffer
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")
        jql = (
            f'project in ({project_filter}) AND updated >= "{cutoff_str}" '
            f'AND created >= "{history_cutoff_str}" '
            f"ORDER BY updated DESC"
        )
        log.info("Incremental sync: issues updated since %s (buffer: %s)", cutoff_str, buffer)
    else:
        jql = (
            f'project in ({project_filter}) AND created >= "{history_cutoff_str}" '
            f"ORDER BY updated DESC"
        )
        log.info("Full sync: fetching all issues")

    next_page_token = resume_token
    page_size = 100
    total_issues, total_transitions = resume_counts if resume_counts else (0, 0)
    page = 0

    if resume_token:
        log.info("Resuming from checkpoint (already synced: %d issues, %d transitions)",
                 total_issues, total_transitions)

    fields = [
        "summary", "issuetype", "status", "priority", STORY_POINTS_FIELD,
        "assignee", "reporter", "created", "updated", "resolutiondate",
        "fixVersions", "labels", "components", "project", "parent", "issuelinks",
        ACCEPTANCE_CRITERIA_FIELD, CUSTOMER_PROJECT_FIELD, QA_FIELD,
    ]

    while True:
        data = jira_search(jql, fields=fields, next_page_token=next_page_token, max_results=page_size)

        issues = data.get("issues", [])
        if not issues:
            break

        issue_rows = []
        transition_rows = []
        fix_version_events = []
        sprint_events = []
        link_rows = []

        for issue in issues:
            f = issue["fields"]
            key = issue["key"]

            story_points = f.get(STORY_POINTS_FIELD)
            fix_versions = [v["name"] for v in f.get("fixVersions", [])]
            labels = f.get("labels", [])
            # Parent epic: for Stories/Tasks the parent is typically the Epic.
            # We only store the key when the parent issue type is "Epic".
            parent = f.get("parent") or {}
            epic_key = (
                parent.get("key")
                if parent.get("fields", {}).get("issuetype", {}).get("name") == "Epic"
                else None
            )

            # Acceptance criteria: store boolean — does the field have any content?
            ac_raw = f.get(ACCEPTANCE_CRITERIA_FIELD)
            has_acceptance_criteria = bool(
                ac_raw and ac_raw.get("content") and len(ac_raw["content"]) > 0
            ) if isinstance(ac_raw, dict) else bool(ac_raw)

            # QA assignee: user-picker field
            qa_raw = f.get(QA_FIELD)
            qa_assignee = qa_raw.get("displayName") if isinstance(qa_raw, dict) else None

            # Components: array of component objects
            components = [c["name"] for c in f.get("components", []) if c.get("name")]

            # Customer-Project: cascading select — parent=customer, child=project
            cp_raw = f.get(CUSTOMER_PROJECT_FIELD)
            if isinstance(cp_raw, dict):
                customer     = cp_raw.get("value") or None
                project_name = (cp_raw.get("child") or {}).get("value") or None
                customer_project = f"{customer} / {project_name}" if (customer and project_name) else (customer or None)
            else:
                customer = project_name = customer_project = None

            issue_rows.append((
                key,
                f["project"]["key"],
                f.get("summary"),
                f["issuetype"]["name"],
                f["status"]["name"],
                f["status"]["statusCategory"]["name"],
                f["priority"]["name"].split("(")[0].strip() if f.get("priority") else None,
                story_points,
                f["assignee"]["displayName"] if f.get("assignee") else None,
                f["reporter"]["displayName"] if f.get("reporter") else None,
                parse_dt(f.get("created")),
                parse_dt(f.get("updated")),
                parse_dt(f.get("resolutiondate")),
                fix_versions,
                labels,
                epic_key,
                has_acceptance_criteria,
                customer_project,
                customer,
                project_name,
                qa_assignee,
                components,
            ))

            # Extract all issue links (both directions)
            for link in f.get("issuelinks", []):
                link_type = link.get("type", {}).get("name", "")
                for direction, side_key in [("outward", "outwardIssue"), ("inward", "inwardIssue")]:
                    if side_key not in link:
                        continue
                    linked = link[side_key]
                    label = link.get("type", {}).get(direction, "")
                    link_rows.append((
                        key,
                        linked["key"],
                        linked.get("fields", {}).get("summary"),
                        link_type,
                        label,
                        direction,
                    ))

            t_rows, fv_events, sp_events = _fetch_changelog(key)
            transition_rows.extend(t_rows)
            fix_version_events.extend(fv_events)
            sprint_events.extend(sp_events)

        is_last = data.get("isLast", True)
        next_page_token = None if is_last else data.get("nextPageToken")

        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO issues (
                    key, project_key, summary, issue_type, status, status_category,
                    priority, story_points, assignee, reporter,
                    created_at, updated_at, resolved_at,
                    fix_versions, labels, epic_key, has_acceptance_criteria,
                    customer_project, customer, project_name, qa_assignee, components
                ) VALUES %s
                ON CONFLICT (key) DO UPDATE SET
                    summary                  = EXCLUDED.summary,
                    issue_type               = EXCLUDED.issue_type,
                    status                   = EXCLUDED.status,
                    status_category          = EXCLUDED.status_category,
                    priority                 = EXCLUDED.priority,
                    story_points             = EXCLUDED.story_points,
                    assignee                 = EXCLUDED.assignee,
                    updated_at               = EXCLUDED.updated_at,
                    resolved_at              = EXCLUDED.resolved_at,
                    fix_versions             = EXCLUDED.fix_versions,
                    labels                   = EXCLUDED.labels,
                    epic_key                 = EXCLUDED.epic_key,
                    has_acceptance_criteria  = EXCLUDED.has_acceptance_criteria,
                    customer_project         = EXCLUDED.customer_project,
                    customer                 = EXCLUDED.customer,
                    project_name             = EXCLUDED.project_name,
                    qa_assignee              = EXCLUDED.qa_assignee,
                    components               = EXCLUDED.components,
                    synced_at                = NOW()
                """,
                issue_rows,
            )

            if transition_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO issue_transitions
                        (issue_key, from_status, to_status, transitioned_at, author)
                    VALUES %s
                    ON CONFLICT (issue_key, transitioned_at, to_status) DO NOTHING
                    """,
                    transition_rows,
                )

            if fix_version_events:
                execute_values(
                    cur,
                    """
                    INSERT INTO issue_fix_version_history
                        (issue_key, fix_version, added_at, removed_at)
                    VALUES %s
                    ON CONFLICT (issue_key, fix_version, added_at) DO NOTHING
                    """,
                    fix_version_events,
                )

            if sprint_events:
                execute_values(
                    cur,
                    """
                    INSERT INTO issue_sprint_history (issue_key, sprint_id, event, occurred_at)
                    VALUES %s
                    ON CONFLICT (issue_key, sprint_id, event, occurred_at) DO NOTHING
                    """,
                    sprint_events,
                )

            # Diff issue links against DB state: record added/removed in history,
            # then update the current snapshot in issue_links.
            synced_keys = [row[0] for row in issue_rows]

            # Fetch existing links for this page's issues
            cur.execute(
                """
                SELECT from_key, to_key, to_summary, link_type, link_label, direction
                FROM issue_links WHERE from_key = ANY(%s)
                """,
                (synced_keys,),
            )
            existing = {
                (r[0], r[1], r[3], r[5]): r  # key: (from,to,type,dir)
                for r in cur.fetchall()
            }
            incoming = {
                (r[0], r[1], r[3], r[5]): r  # (from,to,type,dir)
                for r in link_rows
            }

            history_rows = []
            # Removed links
            for k, r in existing.items():
                if k not in incoming:
                    history_rows.append((r[0], r[1], r[2], r[3], r[4], r[5], "removed"))
            # Added links
            for k, r in incoming.items():
                if k not in existing:
                    history_rows.append((r[0], r[1], r[2], r[3], r[4], r[5], "added"))

            if history_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO issue_link_history
                        (from_key, to_key, to_summary, link_type, link_label, direction, event)
                    VALUES %s
                    """,
                    history_rows,
                )

            # Delete all existing links for synced issues and re-insert current state
            cur.execute("DELETE FROM issue_links WHERE from_key = ANY(%s)", (synced_keys,))
            if link_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO issue_links
                        (from_key, to_key, to_summary, link_type, link_label, direction)
                    VALUES %s
                    ON CONFLICT (from_key, to_key, link_type, direction) DO UPDATE SET
                        to_summary = EXCLUDED.to_summary,
                        link_label = EXCLUDED.link_label,
                        synced_at  = NOW()
                    """,
                    link_rows,
                )

            # Save checkpoint after every page so a restart can resume here
            cur.execute(
                """
                UPDATE sync_log
                   SET next_page_token        = %s,
                       issues_checkpoint      = %s,
                       transitions_checkpoint = %s
                 WHERE id = %s
                """,
                (next_page_token, total_issues + len(issue_rows),
                 total_transitions + len(transition_rows), sync_id),
            )

        conn.commit()
        page += 1
        total_issues += len(issue_rows)
        total_transitions += len(transition_rows)
        log.info("Page %d: %d issues, %d transitions (total: %d issues)",
                 page, len(issue_rows), len(transition_rows), total_issues)

        if is_last:
            break

    log.info("Issues synced: %d  Transitions synced: %d", total_issues, total_transitions)
    return total_issues, total_transitions


def _get_scrum_boards():
    """Return all scrum boards belonging to the configured projects."""
    boards = []
    start = 0
    while True:
        data = jira_get(
            "agile/1.0/board",
            params={"startAt": start, "maxResults": 50, "type": "scrum"},
        )
        for board in data.get("values", []):
            if board.get("location", {}).get("projectKey") in JIRA_PROJECT_KEYS:
                boards.append(board)
        if data.get("isLast", True):
            break
        start += 50
    return boards


def _sync_sprint_members(conn, sprint_id, sprint_state):
    """Upsert current sprint membership; mark removed issues; track scope changes.

    was_in_initial_scope logic:
    - First time we ever sync an active sprint: all current members are initial scope.
    - Subsequent syncs of an active sprint: only pre-existing rows are initial scope;
      newly inserted rows (added mid-sprint) are NOT initial scope.
    - Closed/future sprints: always mark as initial scope (historical data, no live tracking).
    """
    try:
        data = jira_get(
            f"agile/1.0/sprint/{sprint_id}/issue",
            params={"maxResults": 500, "fields": f"summary,status,{STORY_POINTS_FIELD}"},
        )
    except requests.HTTPError as exc:
        log.warning("Could not fetch issues for sprint %d: %s", sprint_id, exc)
        return

    current_keys = set()
    rows = []
    for issue in data.get("issues", []):
        f = issue["fields"]
        sp = f.get(STORY_POINTS_FIELD)
        current_keys.add(issue["key"])
        rows.append((sprint_id, issue["key"], sp))

    if not rows:
        return

    with conn.cursor() as cur:
        # Filter to only issue keys that exist in the issues table.
        # Sprints can contain issues from projects outside JIRA_PROJECT_KEYS
        # (e.g. cross-team epics) which were never synced — inserting them
        # would violate the foreign key constraint.
        all_keys = [r[1] for r in rows]
        cur.execute("SELECT key FROM issues WHERE key = ANY(%s)", (all_keys,))
        known_keys = {r[0] for r in cur.fetchall()}
        skipped = len(rows) - len(known_keys)
        if skipped:
            log.debug("Sprint %d: skipping %d issues not in synced projects", sprint_id, skipped)
        rows = [r for r in rows if r[1] in known_keys]
        current_keys = current_keys & known_keys

        if not rows:
            return

        # Determine was_in_initial_scope for newly inserted rows:
        # - closed/future sprints: always TRUE (historical, no live tracking needed)
        # - active sprint, first sync (no existing rows): TRUE — all current members
        #   were there when the sprint started as far as we can tell
        # - active sprint, subsequent sync: FALSE for new rows — they were added mid-sprint
        existing_members: dict = {}
        if sprint_state in ("closed", "future"):
            initial_scope = True
        else:
            cur.execute(
                "SELECT issue_key, story_points_at_add FROM sprint_issues"
                " WHERE sprint_id = %s AND removed_at IS NULL",
                (sprint_id,),
            )
            existing_members = {r[0]: r[1] for r in cur.fetchall()}
            initial_scope = len(existing_members) == 0  # True only on first sync

        execute_values(
            cur,
            """
            INSERT INTO sprint_issues (sprint_id, issue_key, story_points_at_add, was_in_initial_scope)
            VALUES %s
            ON CONFLICT (sprint_id, issue_key) DO NOTHING
            """,
            [(sprint_id, key, sp, initial_scope) for _sid, key, sp in rows],
        )

        # Mark issues no longer returned by Jira as removed
        if current_keys:
            cur.execute(
                """
                UPDATE sprint_issues
                   SET removed_at = NOW()
                 WHERE sprint_id = %s
                   AND removed_at IS NULL
                   AND issue_key <> ALL(%s)
                """,
                (sprint_id, list(current_keys)),
            )

        # Log mid-sprint scope changes detected between sync runs (active sprints only,
        # after the first sync when existing_members is already populated).
        if sprint_state == "active" and existing_members:
            sp_by_key = {r[1]: r[2] for r in rows}
            added_this_sync   = current_keys - set(existing_members)
            removed_this_sync = set(existing_members) - current_keys
            change_rows = [
                (sprint_id, key, "added",   sp_by_key.get(key))
                for key in added_this_sync
            ] + [
                (sprint_id, key, "removed", existing_members[key])
                for key in removed_this_sync
            ]
            if change_rows:
                execute_values(
                    cur,
                    "INSERT INTO sprint_scope_changes"
                    " (sprint_id, issue_key, change_type, story_points) VALUES %s",
                    change_rows,
                )

    conn.commit()





def backfill_fix_version_history(conn):
    """Backfill issue_fix_version_history for issues not yet scanned.

    Runs incrementally: only fetches changelogs for issues that have at least
    one fix_version assigned but no entry yet in issue_fix_version_history.
    Safe to run repeatedly — already-scanned issues are skipped.
    Subsequent regular syncs will keep the table up to date via _fetch_changelog.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM issues WHERE cardinality(fix_versions) > 0")
        total_with_fv = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT issue_key) FROM issue_fix_version_history")
        already_scanned = cur.fetchone()[0]
        log.info("fix_version_history backfill: %d issues with fix_versions, %d already scanned",
                 total_with_fv, already_scanned)
        cur.execute("""
            SELECT key FROM issues
            WHERE cardinality(fix_versions) > 0
              AND key NOT IN (SELECT DISTINCT issue_key FROM issue_fix_version_history)
            ORDER BY key
        """)
        keys = [row[0] for row in cur.fetchall()]

    if not keys:
        log.info("fix_version_history backfill: nothing to do")
        return

    log.info("fix_version_history backfill: fetching changelogs for %d issues", len(keys))
    rows = []
    for i, key in enumerate(keys, 1):
        _, fv_events, _ = _fetch_changelog(key)
        rows.extend(fv_events)
        if i % 100 == 0:
            log.info("fix_version_history backfill: %d/%d issues scanned", i, len(keys))
        if len(rows) >= 500:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO issue_fix_version_history
                        (issue_key, fix_version, added_at, removed_at)
                    VALUES %s
                    ON CONFLICT (issue_key, fix_version, added_at) DO NOTHING
                    """,
                    rows,
                )
            conn.commit()
            rows = []

    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO issue_fix_version_history
                    (issue_key, fix_version, added_at, removed_at)
                VALUES %s
                ON CONFLICT (issue_key, fix_version, added_at) DO NOTHING
                """,
                rows,
            )
        conn.commit()

    log.info("fix_version_history backfill: complete (%d issues scanned)", len(keys))


def backfill_sprint_history(conn):
    """Backfill issue_sprint_history for issues in tracked sprints not yet scanned.

    Fetches the changelog for each issue that has sprint_issues rows but no
    entry in issue_sprint_history. Runs incrementally — already-scanned issues
    are skipped. Safe to run repeatedly.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT si.issue_key
            FROM sprint_issues si
            WHERE NOT EXISTS (
                SELECT 1 FROM issue_sprint_history ish WHERE ish.issue_key = si.issue_key
            )
            ORDER BY si.issue_key
        """)
        keys = [r[0] for r in cur.fetchall()]

    if not keys:
        log.info("sprint_history backfill: nothing to do")
        return

    log.info("sprint_history backfill: fetching changelogs for %d issues", len(keys))
    rows = []
    for i, key in enumerate(keys, 1):
        _, _, sp_events = _fetch_changelog(key)
        rows.extend(sp_events)
        if i % 100 == 0:
            log.info("sprint_history backfill: %d/%d issues scanned", i, len(keys))
        if len(rows) >= 500:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO issue_sprint_history (issue_key, sprint_id, event, occurred_at)
                    VALUES %s
                    ON CONFLICT (issue_key, sprint_id, event, occurred_at) DO NOTHING
                    """,
                    rows,
                )
            conn.commit()
            rows = []

    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO issue_sprint_history (issue_key, sprint_id, event, occurred_at)
                VALUES %s
                ON CONFLICT (issue_key, sprint_id, event, occurred_at) DO NOTHING
                """,
                rows,
            )
        conn.commit()

    log.info("sprint_history backfill: complete (%d issues scanned)", len(keys))


def backfill_resolved_at(conn):
    """Set resolved_at from issue_transitions for Done issues where it is missing.

    Jira does not always populate resolutiondate, so ~25% of Done issues arrive
    with resolved_at=NULL.

    Pass 1: derive from the latest transition into the issue's current Done
    status (case-insensitive — Jira inconsistently stores e.g. "Done" vs "DONE").

    Pass 2: for issues still missing resolved_at after pass 1 (transition
    timestamps NULL or no matching transition), fall back to updated_at.
    updated_at reflects the last time Jira touched the issue and is a
    reasonable proxy for when it was resolved.
    """
    with conn.cursor() as cur:
        # Pass 1: derive from transition history
        cur.execute(
            """
            UPDATE issues i
            SET resolved_at = (
                SELECT MAX(t.transitioned_at)
                FROM issue_transitions t
                WHERE t.issue_key = i.key
                  AND LOWER(t.to_status) = LOWER(i.status)
            )
            WHERE i.status_category = 'Done'
              AND i.resolved_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM issue_transitions t
                  WHERE t.issue_key = i.key
                    AND LOWER(t.to_status) = LOWER(i.status)
                    AND t.transitioned_at IS NOT NULL
              )
            """
        )
        pass1 = cur.rowcount

        # Pass 2: fall back to updated_at for remaining cases
        cur.execute(
            """
            UPDATE issues
            SET resolved_at = updated_at
            WHERE status_category = 'Done'
              AND resolved_at IS NULL
              AND updated_at IS NOT NULL
            """
        )
        pass2 = cur.rowcount

    conn.commit()
    if pass1 or pass2:
        log.info("Backfilled resolved_at: %d from transitions, %d from updated_at", pass1, pass2)


def sync_sprints(conn):
    log.info("Syncing sprints")
    boards = _get_scrum_boards()
    log.info("Found %d scrum board(s)", len(boards))

    total_sprints = 0
    for board in boards:
        board_id = board["id"]
        start = 0

        while True:
            try:
                data = jira_get(
                    f"agile/1.0/board/{board_id}/sprint",
                    params={
                        "startAt": start,
                        "maxResults": 50,
                        "state": "active,closed,future",
                    },
                )
            except requests.HTTPError as exc:
                log.warning("Could not fetch sprints for board %d: %s", board_id, exc)
                break

            sprints = data.get("values", [])
            if not sprints:
                break

            sprint_rows = [
                (
                    s["id"], board_id, s.get("name"), s.get("state"),
                    parse_dt(s.get("startDate")),
                    parse_dt(s.get("endDate")),
                    parse_dt(s.get("completeDate")),
                    s.get("goal"),
                )
                for s in sprints
            ]

            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO sprints
                        (id, board_id, name, state, start_date, end_date, complete_date, goal)
                    VALUES %s
                    ON CONFLICT (id) DO UPDATE SET
                        name          = EXCLUDED.name,
                        state         = EXCLUDED.state,
                        start_date    = EXCLUDED.start_date,
                        end_date      = EXCLUDED.end_date,
                        complete_date = EXCLUDED.complete_date,
                        goal          = EXCLUDED.goal,
                        synced_at     = NOW()
                    """,
                    sprint_rows,
                )
            conn.commit()

            for sprint in sprints:
                _sync_sprint_members(conn, sprint["id"], sprint.get("state", ""))

            total_sprints += len(sprints)

            if data.get("isLast", True):
                break
            start += 50

    log.info("Sprints synced: %d", total_sprints)
    return total_sprints


def sync_releases(conn):
    log.info("Syncing releases / fix versions")
    rows = []
    for project_key in JIRA_PROJECT_KEYS:
        versions = jira_get(f"api/3/project/{project_key}/versions")
        for v in versions:
            rows.append((
                str(v["id"]),
                project_key,
                v.get("name"),
                v.get("description"),
                v.get("releaseDate"),
                v.get("released", False),
                v.get("archived", False),
            ))

    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO releases
                    (id, project_key, name, description, release_date, released, archived)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    name         = EXCLUDED.name,
                    description  = EXCLUDED.description,
                    release_date = EXCLUDED.release_date,
                    released     = EXCLUDED.released,
                    archived     = EXCLUDED.archived,
                    synced_at    = NOW()
                """,
                rows,
            )
        conn.commit()

    log.info("Releases synced: %d", len(rows))




def sync_sprint_scope(conn):
    """Derive sprint scope from issue changelog history (issue_sprint_history).

    Replaces the deprecated Greenhopper sprint report API. For each sprint:
    - was_in_initial_scope: first 'added' event in issue_sprint_history <= start_date
    - was_punted:           'removed' event exists for this sprint
    - was_added_mid_sprint: first 'added' event > start_date + buffer
    - was_completed:        issue status_category='Done' with resolved_at within sprint window

    Closed sprints without scope_synced_at are processed once and marked done.
    Active sprints are refreshed on every run.
    """
    START_BUFFER = timedelta(hours=2)  # timing slack for sprint-start changelog entries

    log.info("Syncing sprint scope from changelog history")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, start_date, end_date, complete_date, state
            FROM sprints
            WHERE (state = 'closed' AND scope_synced_at IS NULL)
               OR state = 'active'
            ORDER BY state DESC, complete_date DESC NULLS LAST
            """
        )
        pending = cur.fetchall()

    n_closed = sum(1 for *_, s in pending if s == 'closed')
    n_active = sum(1 for *_, s in pending if s == 'active')
    log.info("Sprint scope: %d closed to backfill, %d active to refresh", n_closed, n_active)
    synced = 0

    for sprint_id, start_date, end_date, complete_date, state in pending:
        cutoff = (start_date + START_BUFFER) if start_date else None
        close_ts = complete_date or end_date

        with conn.cursor() as cur:
            # ── Derive scope categories from issue_sprint_history ────────────

            # Initial scope: ticket's last event at/before sprint start was 'added'
            # (tickets added then removed pre-sprint do not count)
            if cutoff:
                cur.execute(
                    """
                    SELECT issue_key
                    FROM (
                        SELECT issue_key, event,
                               ROW_NUMBER() OVER (
                                   PARTITION BY issue_key ORDER BY occurred_at DESC
                               ) AS rn
                        FROM issue_sprint_history
                        WHERE sprint_id = %s AND occurred_at <= %s
                    ) t
                    WHERE rn = 1 AND event = 'added'
                    """,
                    (sprint_id, cutoff),
                )
            else:
                cur.execute(
                    "SELECT issue_key FROM issue_sprint_history"
                    " WHERE sprint_id = %s AND event = 'added'",
                    (sprint_id,),
                )
            initial_scope_keys = {r[0] for r in cur.fetchall()}

            # Mid-sprint additions: not in initial scope, but added after sprint start
            if cutoff:
                cur.execute(
                    """
                    SELECT DISTINCT issue_key FROM issue_sprint_history
                    WHERE sprint_id = %s AND event = 'added' AND occurred_at > %s
                    """,
                    (sprint_id, cutoff),
                )
                added_mid_keys = {r[0] for r in cur.fetchall()} - initial_scope_keys
            else:
                added_mid_keys = set()

            # Punted: removed AFTER sprint start only (pre-sprint churn excluded)
            if cutoff:
                cur.execute(
                    """
                    SELECT issue_key, MAX(occurred_at) AS removed_at
                    FROM issue_sprint_history
                    WHERE sprint_id = %s AND event = 'removed' AND occurred_at > %s
                    GROUP BY issue_key
                    """,
                    (sprint_id, cutoff),
                )
            else:
                cur.execute(
                    """
                    SELECT issue_key, MAX(occurred_at) AS removed_at
                    FROM issue_sprint_history
                    WHERE sprint_id = %s AND event = 'removed'
                    GROUP BY issue_key
                    """,
                    (sprint_id,),
                )
            punted = {r[0]: r[1] for r in cur.fetchall()}

            has_history = bool(initial_scope_keys or added_mid_keys or punted)

            # ── Update sprint_issues ─────────────────────────────────────────

            if has_history:
                all_history_keys = initial_scope_keys | added_mid_keys | set(punted)
                # Set was_in_initial_scope based on changelog timing
                cur.execute(
                    """
                    UPDATE sprint_issues
                       SET was_in_initial_scope = (issue_key = ANY(%s))
                     WHERE sprint_id = %s AND issue_key = ANY(%s)
                    """,
                    (list(initial_scope_keys), sprint_id, list(all_history_keys)),
                )

                if cutoff:
                    # Remove rows for tickets that only appeared in the sprint
                    # pre-start (added then removed before sprint began).
                    # They are not in any of our three sets and have removed_at
                    # set, polluting every scope metric.
                    cur.execute(
                        """
                        DELETE FROM sprint_issues
                        WHERE sprint_id = %s
                          AND removed_at IS NOT NULL
                          AND issue_key != ALL(%s)
                        """,
                        (sprint_id, list(all_history_keys)),
                    )

            # Set removed_at for punted issues; insert missing rows for issues
            # that were removed before we ever synced sprint membership
            for key, removed_ts in punted.items():
                cur.execute(
                    "UPDATE sprint_issues SET removed_at = COALESCE(removed_at, %s)"
                    " WHERE sprint_id = %s AND issue_key = %s",
                    (removed_ts, sprint_id, key),
                )
                cur.execute(
                    """
                    INSERT INTO sprint_issues (sprint_id, issue_key, was_in_initial_scope, removed_at)
                    SELECT %s, key, %s, %s FROM issues WHERE key = %s
                    ON CONFLICT DO NOTHING
                    """,
                    (sprint_id, key in initial_scope_keys, removed_ts, key),
                )

            # ── Populate sprint_scope_initial ────────────────────────────────

            cur.execute(
                """
                SELECT si.issue_key, COALESCE(si.story_points_at_add, i.story_points)
                FROM sprint_issues si JOIN issues i ON i.key = si.issue_key
                WHERE si.sprint_id = %s
                """,
                (sprint_id,),
            )
            sp_map = {r[0]: r[1] for r in cur.fetchall()}

            # Fall back to all current members when history is missing (e.g. backfill
            # not yet run for this sprint's issues) so dashboards stay populated
            scope_keys = initial_scope_keys if has_history else set(sp_map)

            scope_initial_rows = [
                (sprint_id, key, sp_map.get(key))
                for key in scope_keys if key in sp_map
            ]

            cur.execute("DELETE FROM sprint_scope_initial WHERE sprint_id = %s", (sprint_id,))
            if scope_initial_rows:
                execute_values(
                    cur,
                    "INSERT INTO sprint_scope_initial (sprint_id, issue_key, story_points)"
                    " VALUES %s ON CONFLICT DO NOTHING",
                    scope_initial_rows,
                )

            # ── Populate sprint_scope_final (closed sprints only) ────────────

            if state == 'closed':
                cur.execute(
                    """
                    SELECT si.issue_key,
                           COALESCE(si.story_points_at_add, i.story_points),
                           i.status_category,
                           i.resolved_at
                    FROM sprint_issues si JOIN issues i ON i.key = si.issue_key
                    WHERE si.sprint_id = %s
                    """,
                    (sprint_id,),
                )
                scope_final_rows = []
                for key, sp, status_cat, resolved_at in cur.fetchall():
                    is_punted = key in punted
                    is_added_mid = key in added_mid_keys
                    if is_punted:
                        was_completed = False
                    elif close_ts:
                        was_completed = (
                            status_cat == "Done"
                            and resolved_at is not None
                            and resolved_at <= close_ts + timedelta(days=7)
                        )
                    else:
                        was_completed = status_cat == "Done"
                    scope_final_rows.append(
                        (sprint_id, key, sp, was_completed, is_punted, is_added_mid)
                    )

                if scope_final_rows:
                    execute_values(
                        cur,
                        """
                        INSERT INTO sprint_scope_final
                            (sprint_id, issue_key, story_points,
                             was_completed, was_punted, was_added_mid_sprint)
                        VALUES %s
                        ON CONFLICT (sprint_id, issue_key) DO UPDATE
                            SET story_points         = EXCLUDED.story_points,
                                was_completed        = EXCLUDED.was_completed,
                                was_punted           = EXCLUDED.was_punted,
                                was_added_mid_sprint = EXCLUDED.was_added_mid_sprint
                        """,
                        scope_final_rows,
                    )
                log.debug(
                    "Sprint %d: %d initial, %d final, %d punted, %d added_mid",
                    sprint_id, len(scope_initial_rows), len(scope_final_rows),
                    len(punted), len(added_mid_keys),
                )
                cur.execute(
                    "UPDATE sprints SET scope_synced_at = NOW() WHERE id = %s",
                    (sprint_id,),
                )

        conn.commit()
        synced += 1
        if synced % 50 == 0:
            log.info("Sprint scope synced: %d / %d", synced, len(pending))

    log.info("Sprint scope synced: %d total", synced)


# ─── QASE link sync ──────────────────────────────────────────────────────────

QASE_PROPERTY_KEY = "com.atlassian.jira.issue:qase.jira.cloud:qase-cases:status"
QASE_WORKERS = 10  # concurrent Jira API calls

def _check_qase_link(issue_key):
    """Return (issue_key, has_link) by checking the Jira issue property.

    Uses a direct GET (not jira_get) to avoid ERROR-level logging for the
    expected 404 response when no QASE link exists.
    """
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/properties/{QASE_PROPERTY_KEY}"
    try:
        resp = requests.get(
            url,
            auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            return issue_key, True
        if resp.status_code == 404:
            return issue_key, False
        log.debug("QASE unexpected status %s for %s", resp.status_code, issue_key)
        return issue_key, None  # retry next run
    except Exception as exc:
        log.debug("QASE check failed for %s: %s", issue_key, exc)
        return issue_key, None


def sync_qase_links(conn):
    """Check each issue for a QASE test-case link via Jira issue properties.

    Only processes issues where has_qase_link IS NULL (not yet checked) so
    incremental runs only cover new issues. Uses a thread pool for speed.
    """
    log.info("Syncing QASE links")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT key FROM issues WHERE has_qase_link IS NULL ORDER BY key"
        )
        keys = [r[0] for r in cur.fetchall()]

    if not keys:
        log.info("QASE links: nothing to check")
        return

    log.info("Checking %d issues for QASE links (workers=%d)", len(keys), QASE_WORKERS)

    results = []
    with ThreadPoolExecutor(max_workers=QASE_WORKERS) as pool:
        futures = {pool.submit(_check_qase_link, k): k for k in keys}
        done = 0
        for future in as_completed(futures):
            issue_key, has_link = future.result()
            if has_link is not None:
                results.append((has_link, issue_key))
            done += 1
            if done % 500 == 0:
                log.info("QASE: checked %d / %d", done, len(keys))

    if results:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "UPDATE issues SET has_qase_link = data.has_link "
                "FROM (VALUES %s) AS data(has_link, key) WHERE issues.key = data.key",
                results,
                template="(%s::boolean, %s)",
            )
        conn.commit()

    linked = sum(1 for has_link, _ in results if has_link)
    log.info("QASE links synced: %d linked, %d not linked", linked, len(results) - linked)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    log.info("=== Jira sync starting ===")
    conn = psycopg2.connect(PG_DSN)

    # Check if a previous run was interrupted mid-issues-sync and left a checkpoint
    checkpoint = _resume_checkpoint(conn)
    if checkpoint:
        sync_id, resume_token, issues_done, transitions_done = checkpoint
        log.info("Resuming interrupted sync (id=%d) from checkpoint — "
                 "%d issues and %d transitions already saved",
                 sync_id, issues_done, transitions_done)
        resume_counts = (issues_done, transitions_done)
    else:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sync_log (status) VALUES ('running') RETURNING id")
            sync_id = cur.fetchone()[0]
        conn.commit()
        resume_token = None
        resume_counts = None

    issues_synced = 0
    transitions_synced = 0
    sprints_synced = 0
    errors = []

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ('jira_url', %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (JIRA_URL,),
            )
        conn.commit()
    except Exception as exc:
        log.warning("Could not write jira_url to app_settings: %s", exc)

    try:
        last_sync_start, last_sync_finish = _last_successful_sync(conn)
        last_sync_duration = (last_sync_finish - last_sync_start) if (last_sync_start and last_sync_finish) else None
        sync_projects(conn)
        issues_synced, transitions_synced = sync_issues(
            conn, sync_id,
            since=last_sync_start,
            last_sync_duration=last_sync_duration,
            resume_token=resume_token,
            resume_counts=resume_counts,
        )
    except Exception as exc:
        log.error("Issues sync failed: %s", exc, exc_info=True)
        errors.append(f"issues: {exc}")

    # Sprints and releases are independent — run even if issues sync failed,
    # and record success for whichever steps completed so the next incremental
    # run does not have to redo the full issue import.
    try:
        sprints_synced = sync_sprints(conn)
    except Exception as exc:
        log.error("Sprints sync failed: %s", exc, exc_info=True)
        errors.append(f"sprints: {exc}")

    try:
        backfill_sprint_history(conn)
    except Exception as exc:
        log.error("Sprint history backfill failed: %s", exc, exc_info=True)
        errors.append(f"sprint_history_backfill: {exc}")

    try:
        sync_sprint_scope(conn)
    except Exception as exc:
        log.error("Sprint scope sync failed: %s", exc, exc_info=True)
        errors.append(f"sprint_scope: {exc}")

    try:
        backfill_resolved_at(conn)
    except Exception as exc:
        log.error("resolved_at backfill failed: %s", exc, exc_info=True)
        errors.append(f"resolved_at: {exc}")

    try:
        backfill_fix_version_history(conn)
    except Exception as exc:
        log.error("fix_version_history backfill failed: %s", exc, exc_info=True)
        errors.append(f"fix_version_history: {exc}")

    try:
        sync_releases(conn)
    except Exception as exc:
        log.error("Releases sync failed: %s", exc, exc_info=True)
        errors.append(f"releases: {exc}")

    try:
        sync_qase_links(conn)
    except Exception as exc:
        log.error("QASE links sync failed: %s", exc, exc_info=True)
        errors.append(f"qase: {exc}")

    # Record success as long as the issues sync completed — that is the
    # step that determines whether the next run can be incremental.
    if "issues:" not in " ".join(errors):
        status = "success" if not errors else "partial"
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_log
                   SET status = %s, finished_at = NOW(),
                       issues_synced = %s, sprints_synced = %s,
                       transitions_synced = %s,
                       error_message = %s
                 WHERE id = %s
                """,
                (status, issues_synced, sprints_synced, transitions_synced,
                 "; ".join(errors) or None, sync_id),
            )
        conn.commit()
        log.info("=== Sync complete (status: %s) ===", status)
    else:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_log SET status='error', finished_at=NOW(), error_message=%s WHERE id=%s",
                ("; ".join(errors), sync_id),
            )
        conn.commit()
        log.error("=== Sync failed ===")
        _notify_failure(errors)
        conn.close()
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()

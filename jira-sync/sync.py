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

PG_DSN = (
    f"host={os.environ['POSTGRES_HOST']} "
    f"dbname={os.environ['POSTGRES_DB']} "
    f"user={os.environ['POSTGRES_USER']} "
    f"password={os.environ['POSTGRES_PASSWORD']}"
)


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


def _fetch_changelog(issue_key):
    """Fetch all status transitions for a single issue via the changelog endpoint.

    Jira Cloud deprecated expand=changelog on the bulk search endpoint (410).
    This calls the dedicated per-issue changelog API instead.

    Some issues are corrupt on Jira's side (internal PSQLException returned as
    a 400) — these are logged and skipped rather than failing the entire sync.
    """
    transition_rows = []
    start = 0
    while True:
        try:
            data = jira_get(
                f"api/3/issue/{issue_key}/changelog",
                params={"startAt": start, "maxResults": 100},
            )
        except requests.exceptions.HTTPError as exc:
            log.warning("Skipping changelog for %s — Jira returned %s", issue_key, exc)
            return []
        for history in data.get("values", []):
            for item in history.get("items", []):
                if item["field"] == "status":
                    transition_rows.append((
                        issue_key,
                        item.get("fromString"),
                        item.get("toString"),
                        parse_dt(history.get("created")),
                        history.get("author", {}).get("displayName"),
                    ))
        if data.get("isLast", True):
            break
        start += data.get("maxResults", 100)
    return transition_rows


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

    # Hard limit: never sync issues older than JIRA_MAX_HISTORY_YEARS (default 3)
    max_years = int(os.environ.get("JIRA_MAX_HISTORY_YEARS", "3"))
    history_cutoff = datetime.now(timezone.utc) - timedelta(days=max_years * 365)
    history_cutoff_str = history_cutoff.strftime("%Y-%m-%d")

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
        "fixVersions", "labels", "project",
    ]

    while True:
        data = jira_search(jql, fields=fields, next_page_token=next_page_token, max_results=page_size)

        issues = data.get("issues", [])
        if not issues:
            break

        issue_rows = []
        transition_rows = []

        for issue in issues:
            f = issue["fields"]
            key = issue["key"]

            story_points = f.get(STORY_POINTS_FIELD)
            fix_versions = [v["name"] for v in f.get("fixVersions", [])]
            labels = f.get("labels", [])

            issue_rows.append((
                key,
                f["project"]["key"],
                f.get("summary"),
                f["issuetype"]["name"],
                f["status"]["name"],
                f["status"]["statusCategory"]["name"],
                f["priority"]["name"] if f.get("priority") else None,
                story_points,
                f["assignee"]["displayName"] if f.get("assignee") else None,
                f["reporter"]["displayName"] if f.get("reporter") else None,
                parse_dt(f.get("created")),
                parse_dt(f.get("updated")),
                parse_dt(f.get("resolutiondate")),
                fix_versions,
                labels,
            ))

            transition_rows.extend(_fetch_changelog(key))

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
                    fix_versions, labels
                ) VALUES %s
                ON CONFLICT (key) DO UPDATE SET
                    summary          = EXCLUDED.summary,
                    issue_type       = EXCLUDED.issue_type,
                    status           = EXCLUDED.status,
                    status_category  = EXCLUDED.status_category,
                    priority         = EXCLUDED.priority,
                    story_points     = EXCLUDED.story_points,
                    assignee         = EXCLUDED.assignee,
                    updated_at       = EXCLUDED.updated_at,
                    resolved_at      = EXCLUDED.resolved_at,
                    fix_versions     = EXCLUDED.fix_versions,
                    labels           = EXCLUDED.labels,
                    synced_at        = NOW()
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


def _sync_sprint_members(conn, sprint_id):
    """Upsert current sprint membership; mark removed issues."""
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

        # Upsert membership
        execute_values(
            cur,
            """
            INSERT INTO sprint_issues (sprint_id, issue_key, story_points_at_add)
            VALUES %s
            ON CONFLICT (sprint_id, issue_key) DO NOTHING
            """,
            rows,
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

    conn.commit()


def _take_sprint_snapshot(conn, sprint):
    """Record a start/close snapshot for planning-deviation tracking."""
    sprint_id = sprint["id"]
    state = sprint.get("state", "")

    if state == "active":
        snapshot_type = "start"
    elif state == "closed":
        snapshot_type = "close"
    else:
        return

    with conn.cursor() as cur:
        # Check if we already have this snapshot
        cur.execute(
            "SELECT 1 FROM sprint_snapshots WHERE sprint_id = %s AND snapshot_type = %s",
            (sprint_id, snapshot_type),
        )
        if cur.fetchone():
            return  # don't overwrite existing snapshots

        cur.execute(
            """
            INSERT INTO sprint_snapshots (
                sprint_id, snapshot_type,
                total_issues, total_story_points,
                completed_issues, completed_story_points
            )
            SELECT
                %s, %s,
                COUNT(*),
                COALESCE(SUM(i.story_points), 0),
                COUNT(*) FILTER (WHERE i.status_category = 'Done'),
                COALESCE(SUM(i.story_points) FILTER (WHERE i.status_category = 'Done'), 0)
            FROM sprint_issues si
            JOIN issues i ON i.key = si.issue_key
            WHERE si.sprint_id = %s
              AND si.removed_at IS NULL
            """,
            (sprint_id, snapshot_type, sprint_id),
        )
    conn.commit()


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
                _sync_sprint_members(conn, sprint["id"])
                _take_sprint_snapshot(conn, sprint)

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
        sync_releases(conn)
    except Exception as exc:
        log.error("Releases sync failed: %s", exc, exc_info=True)
        errors.append(f"releases: {exc}")

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
        conn.close()
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()

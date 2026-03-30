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
    """Raised when code attempts any non-GET call to Jira."""


class ReadOnlyJiraSession(requests.Session):
    """A requests Session that hard-blocks every mutating HTTP method.

    POST, PUT, PATCH, and DELETE will raise JiraWriteAttemptError before any
    network connection is made, so Jira data can never be changed regardless
    of what the rest of the code does.
    """

    _BLOCKED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def request(self, method, url, **kwargs):
        if method.upper() in self._BLOCKED_METHODS:
            raise JiraWriteAttemptError(
                f"Blocked attempt to call {method.upper()} {url} — "
                "this script is read-only and must never modify Jira."
            )
        return super().request(method, url, **kwargs)


_jira_session = ReadOnlyJiraSession()
_jira_session.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
_jira_session.headers.update({"Accept": "application/json"})


# ─── Jira API helpers ────────────────────────────────────────────────────────

def jira_get(path, params=None):
    """Perform a GET request against the Jira REST API.

    This is the single entry-point for all Jira communication. It intentionally
    exposes only GET so call-sites cannot accidentally pass a mutating method.
    """
    url = f"{JIRA_URL}/rest/{path}"
    resp = _jira_session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


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
            INSERT INTO projects (key, name, synced_at)
            VALUES %s
            ON CONFLICT (key) DO UPDATE
                SET name = EXCLUDED.name, synced_at = NOW()
            """,
            rows,
        )
    conn.commit()
    log.info("Synced %d project(s)", len(rows))


def sync_issues(conn):
    """Sync all issues and their changelog (status transitions)."""
    log.info("Syncing issues")
    project_filter = ", ".join(JIRA_PROJECT_KEYS)
    jql = f"project in ({project_filter}) ORDER BY updated DESC"
    start = 0
    page_size = 100
    total_issues = 0
    total_transitions = 0

    while True:
        data = jira_get(
            "api/3/search",
            params={
                "jql": jql,
                "startAt": start,
                "maxResults": page_size,
                "fields": (
                    f"summary,issuetype,status,priority,{STORY_POINTS_FIELD},"
                    "assignee,reporter,created,updated,resolutiondate,"
                    "fixVersions,labels,project"
                ),
                "expand": "changelog",
            },
        )

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

            for history in issue.get("changelog", {}).get("histories", []):
                for item in history.get("items", []):
                    if item["field"] == "status":
                        transition_rows.append((
                            key,
                            item.get("fromString"),
                            item.get("toString"),
                            parse_dt(history.get("created")),
                            history.get("author", {}).get("displayName"),
                        ))

        with conn.cursor() as cur:
            execute_values(
                cur,
                f"""
                INSERT INTO issues (
                    key, project_key, summary, issue_type, status, status_category,
                    priority, story_points, assignee, reporter,
                    created_at, updated_at, resolved_at,
                    fix_versions, labels, synced_at
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

        conn.commit()
        total_issues += len(issue_rows)
        total_transitions += len(transition_rows)
        log.info(
            "Page %d–%d: %d issues, %d transitions",
            start + 1, start + len(issues), len(issue_rows), len(transition_rows),
        )

        if start + page_size >= data["total"]:
            break
        start += page_size

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
                        (id, board_id, name, state, start_date, end_date, complete_date, goal, synced_at)
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
                    (id, project_key, name, description, release_date, released, archived, synced_at)
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

    with conn.cursor() as cur:
        cur.execute("INSERT INTO sync_log (status) VALUES ('running') RETURNING id")
        sync_id = cur.fetchone()[0]
    conn.commit()

    try:
        sync_projects(conn)
        issues_synced, transitions_synced = sync_issues(conn)
        sprints_synced = sync_sprints(conn)
        sync_releases(conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_log
                   SET status = 'success', finished_at = NOW(),
                       issues_synced = %s, sprints_synced = %s,
                       transitions_synced = %s
                 WHERE id = %s
                """,
                (issues_synced, sprints_synced, transitions_synced, sync_id),
            )
        conn.commit()
        log.info("=== Sync complete ===")

    except Exception as exc:
        log.error("Sync failed: %s", exc, exc_info=True)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_log SET status='error', finished_at=NOW(), error_message=%s WHERE id=%s",
                (str(exc), sync_id),
            )
        conn.commit()
        sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()

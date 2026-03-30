#!/usr/bin/env python3
"""One-off backfill: re-fetch story points for all issues and rebuild sprint snapshots.

READ-ONLY from Jira — only fetches issue fields, no changelog.
Much faster than a full re-sync (~300 API calls vs 27,000+).

Run with:
  docker compose exec jira-sync python backfill_story_points.py
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
import requests
from psycopg2.extras import execute_values
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEYS = [k.strip() for k in os.environ["JIRA_PROJECT_KEYS"].split(",")]
STORY_POINTS_FIELD = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016")

PG_DSN = (
    f"host={os.environ['POSTGRES_HOST']} "
    f"dbname={os.environ['POSTGRES_DB']} "
    f"user={os.environ['POSTGRES_USER']} "
    f"password={os.environ['POSTGRES_PASSWORD']}"
)

session = requests.Session()
session.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})


def jira_post(path, payload, retries=5):
    url = f"{JIRA_URL}{path}"
    for attempt in range(retries):
        try:
            r = session.post(url, json=payload, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                log.warning("HTTP %s, retrying in %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ConnectionError as e:
            wait = 2 ** attempt
            log.warning("Connection error: %s, retrying in %ss", e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {retries} retries: {url}")


def backfill_story_points(conn):
    project_filter = ", ".join(f'"{k}"' for k in JIRA_PROJECT_KEYS)
    jql = f"project in ({project_filter}) ORDER BY updated DESC"

    log.info("Using story points field: %s", STORY_POINTS_FIELD)
    log.info("Fetching all issues to backfill story points...")

    next_page_token = None
    page = 0
    total_updated = 0

    while True:
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": [STORY_POINTS_FIELD, "key"],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        data = jira_post("/rest/api/3/search/jql", payload)
        issues = data.get("issues", [])
        if not issues:
            break

        rows = []
        for issue in issues:
            f = issue.get("fields", {})
            sp = f.get(STORY_POINTS_FIELD)
            if sp is not None:
                try:
                    rows.append((float(sp), issue["key"]))
                except (TypeError, ValueError):
                    pass

        if rows:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    "UPDATE issues SET story_points = data.sp FROM (VALUES %s) AS data(sp, key) WHERE issues.key = data.key",
                    rows,
                    template="(%s::numeric, %s::text)",
                )
            conn.commit()
            total_updated += len(rows)

        page += 1
        if page % 10 == 0:
            log.info("Page %d: updated %d issues so far", page, total_updated)

        if data.get("isLast", True) or not data.get("nextPageToken"):
            break
        next_page_token = data["nextPageToken"]

    log.info("Story points backfill complete: %d issues updated", total_updated)
    return total_updated


def rebuild_sprint_snapshots(conn):
    log.info("Rebuilding sprint snapshots...")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sprint_snapshots")
        cur.execute("""
            INSERT INTO sprint_snapshots (
                sprint_id, snapshot_type, snapshot_at,
                total_issues, total_story_points,
                completed_issues, completed_story_points
            )
            SELECT
                s.id,
                'start',
                COALESCE(s.start_date, NOW()),
                COUNT(si.issue_key),
                COALESCE(SUM(i.story_points), 0),
                0,
                0
            FROM sprints s
            JOIN sprint_issues si ON si.sprint_id = s.id
            JOIN issues i ON i.key = si.issue_key
            GROUP BY s.id
        """)
        cur.execute("""
            INSERT INTO sprint_snapshots (
                sprint_id, snapshot_type, snapshot_at,
                total_issues, total_story_points,
                completed_issues, completed_story_points
            )
            SELECT
                s.id,
                'close',
                COALESCE(s.complete_date, NOW()),
                COUNT(si.issue_key),
                COALESCE(SUM(i.story_points), 0),
                COUNT(i.key) FILTER (WHERE i.status_category = 'Done'),
                COALESCE(SUM(i.story_points) FILTER (WHERE i.status_category = 'Done'), 0)
            FROM sprints s
            JOIN sprint_issues si ON si.sprint_id = s.id
            JOIN issues i ON i.key = si.issue_key
            GROUP BY s.id
        """)
        conn.commit()

        cur.execute("SELECT COUNT(*), AVG(total_story_points) FROM sprint_snapshots WHERE snapshot_type = 'start'")
        count, avg_pts = cur.fetchone()
        log.info("Sprint snapshots rebuilt: %d sprints, avg %.1f story points", count, avg_pts or 0)


def main():
    conn = psycopg2.connect(PG_DSN)
    try:
        backfill_story_points(conn)
        rebuild_sprint_snapshots(conn)
        log.info("All done. Refresh your Grafana dashboards.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

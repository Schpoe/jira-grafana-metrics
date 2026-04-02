#!/usr/bin/env python3
"""One-off check: find Epics that have an 'Implements' issue link to a PROD item.

Usage (from project root):
    docker compose run --rm jira-sync python check_epic_implements.py

Output: table of Epic key, Epic summary, linked PROD key, link direction.
"""

import os
import sys
import time
import requests
from requests.auth import HTTPBasicAuth

JIRA_URL        = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL      = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN  = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEYS = [k.strip() for k in os.environ["JIRA_PROJECT_KEYS"].split(",")]

session = requests.Session()
session.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
session.headers.update({"Accept": "application/json"})


def jira_get(url, params=None):
    for attempt in range(5):
        r = session.get(url, params=params, timeout=30)
        if r.status_code in (429,) or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed: GET {url}")


def fetch_all_epics():
    project_filter = ", ".join(f'"{k}"' for k in JIRA_PROJECT_KEYS)
    jql = f'project in ({project_filter}) AND issuetype = Epic ORDER BY key ASC'
    start = 0
    page_size = 100
    epics = []
    while True:
        data = jira_get(
            f"{JIRA_URL}/rest/api/3/search",
            params={
                "jql": jql,
                "startAt": start,
                "maxResults": page_size,
                "fields": "summary,issuelinks",
            },
        )
        issues = data.get("issues", [])
        epics.extend(issues)
        start += len(issues)
        if start >= data["total"]:
            break
    return epics


def main():
    print(f"Fetching all Epics from projects: {', '.join(JIRA_PROJECT_KEYS)} …")
    epics = fetch_all_epics()
    print(f"Found {len(epics)} Epics. Checking for 'Implements' links to PROD …\n")

    hits = []
    for epic in epics:
        key     = epic["key"]
        summary = epic["fields"].get("summary", "")
        links   = epic["fields"].get("issuelinks", [])
        for link in links:
            link_type = link.get("type", {}).get("name", "")
            if "implements" not in link_type.lower():
                continue

            # outward: this Epic implements something
            if "outwardIssue" in link:
                linked = link["outwardIssue"]
                direction = "outward"
            # inward: something implements this Epic
            elif "inwardIssue" in link:
                linked = link["inwardIssue"]
                direction = "inward"
            else:
                continue

            linked_key = linked["key"]
            linked_summary = linked.get("fields", {}).get("summary", "")

            if linked_key.startswith("PROD"):
                hits.append({
                    "epic_key":       key,
                    "epic_summary":   summary,
                    "direction":      direction,
                    "link_type":      link_type,
                    "prod_key":       linked_key,
                    "prod_summary":   linked_summary,
                })

    if not hits:
        print("No Epics found with an 'Implements' link to a PROD item.")
        return

    print(f"Found {len(hits)} Implements link(s) to PROD:\n")
    print(f"{'Epic':<15} {'Direction':<10} {'PROD Item':<15} {'Epic Summary':<50} {'PROD Summary'}")
    print("-" * 130)
    for h in hits:
        print(
            f"{h['epic_key']:<15} {h['direction']:<10} {h['prod_key']:<15} "
            f"{h['epic_summary'][:48]:<50} {h['prod_summary'][:50]}"
        )


if __name__ == "__main__":
    main()

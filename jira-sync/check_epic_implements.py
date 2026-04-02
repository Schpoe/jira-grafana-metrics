#!/usr/bin/env python3
"""Diagnostic: find how Epics reference PROD items — via issuelinks or custom fields.

Usage (from project root):
    docker compose run --rm jira-sync python check_epic_implements.py

Outputs:
  1. ALL issue link types found on Epics that point to PROD-* keys
  2. Any custom fields on Epics whose value looks like a PROD-* key
  3. Raw field dump of the first Epic that has any PROD link (for inspection)
"""

import json
import os
import re
import time
import requests
from requests.auth import HTTPBasicAuth

JIRA_URL          = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL        = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN    = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEYS = [k.strip() for k in os.environ["JIRA_PROJECT_KEYS"].split(",")]

PROD_KEY_RE = re.compile(r'\bPROD-\d+\b')

session = requests.Session()
session.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
session.headers.update({"Accept": "application/json"})


def jira_post(url, payload):
    for attempt in range(5):
        r = session.post(url, json=payload, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed: POST {url}")


def fetch_all_epics():
    """Fetch all Epics with ALL fields so we can inspect custom fields too."""
    project_filter = ", ".join(f'"{k}"' for k in JIRA_PROJECT_KEYS)
    jql = f'project in ({project_filter}) AND issuetype = Epic ORDER BY key ASC'
    epics = []
    next_page_token = None
    while True:
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": ["summary", "issuelinks", "*all"],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        data = jira_post(f"{JIRA_URL}/rest/api/3/search/jql", payload)
        issues = data.get("issues", [])
        epics.extend(issues)
        next_page_token = data.get("nextPageToken")
        if not next_page_token or not issues:
            break
    return epics


def find_prod_refs_in_value(value):
    """Recursively search any field value for PROD-* key strings."""
    if isinstance(value, str):
        return PROD_KEY_RE.findall(value)
    if isinstance(value, dict):
        found = []
        for v in value.values():
            found.extend(find_prod_refs_in_value(v))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(find_prod_refs_in_value(item))
        return found
    return []


def main():
    print(f"Fetching all Epics from projects: {', '.join(JIRA_PROJECT_KEYS)} …")
    epics = fetch_all_epics()
    print(f"Found {len(epics)} Epics.\n")

    link_hits   = []   # via issuelinks
    custom_hits = []   # via custom fields
    first_hit_fields = None

    for epic in epics:
        key     = epic["key"]
        summary = epic["fields"].get("summary", "")
        fields  = epic["fields"]

        # ── 1. issuelinks (any link type) ────────────────────────────────────
        for link in fields.get("issuelinks", []):
            link_type = link.get("type", {}).get("name", "")
            inward_label  = link.get("type", {}).get("inward", "")
            outward_label = link.get("type", {}).get("outward", "")

            for direction, side_key in [("outward", "outwardIssue"), ("inward", "inwardIssue")]:
                if side_key not in link:
                    continue
                linked     = link[side_key]
                linked_key = linked["key"]
                linked_sum = linked.get("fields", {}).get("summary", "")
                if linked_key.startswith("PROD"):
                    label = outward_label if direction == "outward" else inward_label
                    link_hits.append({
                        "epic_key":     key,
                        "epic_summary": summary,
                        "direction":    direction,
                        "link_type":    link_type,
                        "link_label":   label,
                        "prod_key":     linked_key,
                        "prod_summary": linked_sum,
                    })
                    if first_hit_fields is None:
                        first_hit_fields = (key, fields)

        # ── 2. custom fields containing PROD-* strings ───────────────────────
        for field_id, value in fields.items():
            if not field_id.startswith("customfield_"):
                continue
            if value is None:
                continue
            prod_refs = find_prod_refs_in_value(value)
            for ref in set(prod_refs):
                custom_hits.append({
                    "epic_key":   key,
                    "field_id":   field_id,
                    "prod_ref":   ref,
                    "raw_value":  str(value)[:120],
                })
                if first_hit_fields is None:
                    first_hit_fields = (key, fields)

    # ── Report: issuelinks ────────────────────────────────────────────────────
    if link_hits:
        print(f"=== PROD links via issuelinks ({len(link_hits)} found) ===\n")
        print(f"{'Epic':<15} {'Link type':<25} {'Label':<25} {'Dir':<8} {'PROD key':<12} {'PROD summary'}")
        print("-" * 120)
        for h in link_hits:
            print(
                f"{h['epic_key']:<15} {h['link_type']:<25} {h['link_label']:<25} "
                f"{h['direction']:<8} {h['prod_key']:<12} {h['prod_summary'][:40]}"
            )
    else:
        print("No PROD links found via issuelinks on any Epic.")

    print()

    # ── Report: custom fields ─────────────────────────────────────────────────
    if custom_hits:
        print(f"=== PROD references in custom fields ({len(custom_hits)} found) ===\n")
        print(f"{'Epic':<15} {'Field ID':<30} {'PROD ref':<12} Raw value")
        print("-" * 110)
        for h in custom_hits:
            print(f"{h['epic_key']:<15} {h['field_id']:<30} {h['prod_ref']:<12} {h['raw_value']}")
    else:
        print("No PROD references found in any custom fields on Epics.")

    print()

    # ── Raw field dump of first matched Epic ─────────────────────────────────
    if first_hit_fields:
        hit_key, hit_fields = first_hit_fields
        non_null = {k: v for k, v in hit_fields.items() if v is not None and v != [] and v != {}}
        print(f"=== Raw fields for {hit_key} (non-null only) ===\n")
        print(json.dumps(non_null, indent=2, default=str)[:4000])


if __name__ == "__main__":
    main()

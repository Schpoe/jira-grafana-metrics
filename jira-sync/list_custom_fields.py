#!/usr/bin/env python3
"""List all Jira custom fields with their IDs and names.
Run inside Docker the same way as sync.py:

  docker compose exec jira-sync python list_custom_fields.py

Optionally filter by keyword:
  docker compose exec jira-sync python list_custom_fields.py customer
"""
import os, sys, requests
from requests.auth import HTTPBasicAuth

JIRA_URL   = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_TOKEN = os.environ["JIRA_API_TOKEN"]

auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
r = requests.get(f"{JIRA_URL}/rest/api/3/field", auth=auth)
r.raise_for_status()

keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None

fields = [f for f in r.json() if f["id"].startswith("customfield_")]
fields.sort(key=lambda f: f["name"].lower())

print(f"\n{'ID':<25} {'Name'}")
print("-" * 60)
for f in fields:
    if keyword and keyword not in f["name"].lower():
        continue
    print(f"{f['id']:<25} {f['name']}")
print()

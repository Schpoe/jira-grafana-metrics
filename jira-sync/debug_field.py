#!/usr/bin/env python3
"""Show the raw Jira value for a custom field on a few epics.
Usage: docker compose exec jira-sync python debug_field.py customfield_10662
"""
import os, sys, json, requests
from requests.auth import HTTPBasicAuth

JIRA_URL   = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_TOKEN = os.environ["JIRA_API_TOKEN"]
PROJECTS   = [k.strip() for k in os.environ["JIRA_PROJECT_KEYS"].split(",")]

field = sys.argv[1] if len(sys.argv) > 1 else "customfield_10662"
auth  = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)

jql = f"project in ({','.join(PROJECTS)}) AND issuetype = Epic ORDER BY updated DESC"
r = requests.get(
    f"{JIRA_URL}/rest/api/3/search",
    params={"jql": jql, "maxResults": 10, "fields": f"summary,{field}"},
    auth=auth,
)
r.raise_for_status()

for issue in r.json().get("issues", []):
    raw = issue["fields"].get(field)
    print(f"{issue['key']:15s}  raw={json.dumps(raw)}")

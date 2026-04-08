# Jira Grafana Metrics

A self-hosted metrics platform that syncs Jira Cloud data into PostgreSQL and visualises it in Grafana.

## Architecture

```text
Jira Cloud → jira-sync (Python) → PostgreSQL → Grafana
```

- **jira-sync**: Python service that pulls issues, sprints, transitions, and releases from the Jira Cloud REST API and upserts them into PostgreSQL. Runs on a cron schedule (07:00 and 19:00 UTC) and supports incremental + full sync.
- **PostgreSQL**: Stores all Jira data plus derived views (`v_planning_deviation`, `v_lead_time`, `v_prod_epic_progress`).
- **Grafana**: Dashboards provisioned from JSON files in `grafana/provisioning/dashboards/`. Auto-reloads every 30 seconds.

## Dashboards

| Dashboard | Description |
| --- | --- |
| Sprint Detail | Per-sprint breakdown: story points, burndown, scope changes, readiness (SP/Epic/AC), QASE coverage, velocity, data quality |
| Sprint Overview Quarter | Cross-sprint velocity, delivery %, scope change trends per quarter |
| Flow & Cycle Time | Lead time, cycle time, throughput |
| Team Overview | Per-team summary: completed issues, assignee breakdown, cycle time, time in status |
| PO KPIs | Planning accuracy, scope change %, blockers, velocity |
| PROD Alignment | Epic progress, customer-project tracking |
| Quality & Bugs | Bug counts, reopen rates, QASE coverage |

## Setup

### Prerequisites

- Docker + Docker Compose
- Jira Cloud account with API token

### Configuration

Copy `.env.example` to `.env` and fill in:

```env
JIRA_URL=https://your-org.atlassian.net
JIRA_EMAIL=your@email.com
JIRA_API_TOKEN=your_api_token
JIRA_PROJECT_KEYS=PROJ1,PROJ2
JIRA_STORY_POINTS_FIELD=customfield_10016
JIRA_HISTORY_START=2024-01-01
POSTGRES_PASSWORD=your_db_password
GF_SECURITY_ADMIN_PASSWORD=your_grafana_password
```

### Start

```bash
docker compose up -d
```

Grafana is available at `http://localhost:3000`.

### Initial data load

```bash
docker compose exec -e FULL_SYNC=1 jira-sync python sync.py
```

### Subsequent syncs

Run automatically at 07:00 and 19:00 UTC. Trigger manually:

```bash
docker compose exec jira-sync python sync.py
```

## Custom fields synced

| Field | Jira ID | Column |
| --- | --- | --- |
| Story Points | `customfield_10016` (configurable) | `story_points` |
| Acceptance Criteria | `customfield_10028` (configurable) | `has_acceptance_criteria` |
| Customer-Project | `customfield_10662` | `customer`, `project_name`, `customer_project` |

The Customer-Project field is a cascading select. Both parent (customer) and child (project) values are stored separately.

## Utilities

```bash
# List all Jira custom fields
docker compose exec jira-sync python list_custom_fields.py

# Filter by keyword
docker compose exec jira-sync python list_custom_fields.py customer

# Inspect raw value of a custom field on recent epics
docker compose exec jira-sync python debug_field.py customfield_10662
```

## Updating dashboards

Grafana reads dashboard JSON files from `grafana/provisioning/dashboards/` and reloads every 30 seconds.

To save changes made in the Grafana UI:

1. Open dashboard → Share → Export → Save to file
2. Overwrite the corresponding file in `grafana/provisioning/dashboards/`
3. Commit and push

## Sprint data model

### Tables

**`sprints`** — one row per Jira sprint, sourced from each board's sprint list.

**`sprint_issues`** — many-to-many between sprints and issues, with scope-change metadata:

| Column | Meaning |
| --- | --- |
| `was_in_initial_scope` | `TRUE` = committed at sprint start; `FALSE` = added mid-sprint |
| `removed_at` | `NULL` = still in sprint; timestamp = punted/removed mid-sprint |
| `story_points_at_add` | SP value at the moment the issue entered the sprint |

### How sprint membership is populated

**Pass 1 — live membership** (`_sync_sprint_members`, every sync):
Calls `/agile/1.0/sprint/{id}/issue` and upserts current members into `sprint_issues`.

- Active sprint, first sync: all current members get `was_in_initial_scope = TRUE`
- Active sprint, subsequent syncs: new rows get `was_in_initial_scope = FALSE` (added mid-sprint)
- Closed/future sprints: always `TRUE` (historical, no live tracking)
- Issues no longer returned by Jira get `removed_at = NOW()`

**Pass 2 — sprint report correction** (`sync_sprint_reports`, once per closed sprint):
Calls the Jira internal sprint report API (`/greenhopper/1.0/rapid/charts/sprintreport`).

- `issueKeysAddedDuringSprint` → corrects those rows to `was_in_initial_scope = FALSE`
- `puntedIssues` → sets `removed_at` on those rows
- Runs once per sprint; `report_synced_at` on the `sprints` table prevents re-processing

### How velocity and delivery are calculated

`v_planning_deviation` computes per-sprint:

- **`committed_points`** — SUM of `story_points_at_add` for issues where `was_in_initial_scope = TRUE`, excluding Epics and Sub-tasks
- **`delivered_points`** — SUM of SP for Done issues where `removed_at IS NULL` and the issue does **not** appear in any later sprint (prevents carry-over double-counting), excluding Epics and Sub-tasks
- **`delivery_pct`** — `delivered_points / committed_points × 100`

The Avg Velocity panel in Sprint Detail takes the average `delivered_points` of the last 6 closed sprints on the **same board** where the majority of issues belong to the same project(s) as the selected sprint. This double filter (board + project majority) handles the case where two teams share a single Jira board.

### Known limitations

- **Active sprint scope tracking is heuristic**: `was_in_initial_scope` is inferred from sync timing for active sprints. The sprint report pass (Pass 2) corrects this for closed sprints using authoritative Jira data.
- **`resolved_at` missing on ~25% of Done issues**: some issues were resolved without a tracked status transition. These issues are still counted in `delivered_points` but cannot be precisely dated.
- **Cross-team boards**: if two teams share a Jira board, sprint velocity filters by project majority (>50% of sprint issues from the selected project) to exclude the other team's sprints.

## Applying schema changes

After a `git pull` that includes `init.sql` changes, re-run the file to apply new columns and views (all statements are idempotent):

```bash
docker compose exec postgres psql -U metrics -d jira_metrics -f /docker-entrypoint-initdb.d/init.sql
```

No restart required for view-only changes. New columns require a full sync to backfill existing issues:

```bash
docker compose exec -e FULL_SYNC=1 jira-sync python sync.py
```

## Database

Direct access:

```bash
docker compose exec postgres psql -U metrics -d jira_metrics
```

# Jira Grafana Metrics

A self-hosted metrics platform that syncs Jira Cloud data into PostgreSQL and visualises it in Grafana.

## Architecture

```text
Jira Cloud → jira-sync (Python) → PostgreSQL → Grafana
```

- **jira-sync**: Python service that pulls issues, sprints, transitions, releases, and fix-version history from the Jira Cloud REST API and upserts them into PostgreSQL. Runs on a cron schedule (07:00 and 19:00 UTC) and supports incremental + full sync.
- **PostgreSQL**: Stores all Jira data plus derived views (`v_planning_deviation`, `v_lead_time`, `v_cycle_time_rft_to_done`, `v_cycle_time_in_progress_to_rft`, `v_time_in_status`, `v_prod_epic_progress`, `v_prod_item_progress`).
- **Grafana**: Dashboards provisioned from JSON files in `grafana/provisioning/dashboards/`. `allowUiUpdates: false` keeps provisioned files authoritative.

## Dashboards

| Dashboard | Description |
| --- | --- |
| Home | Landing page with quick-stats and navigation links |
| Sprint Detail | Per-sprint: story points, burndown, scope changes, story readiness (SP/Epic/AC/Assignee), cross-team dependencies, QASE coverage, velocity |
| Sprint Overview Quarter | Cross-sprint velocity, delivery %, scope change trends per quarter |
| Flow & Cycle Time | Lead time, cycle time (RFT→Done, In Progress→RFT), throughput |
| Team Overview | Per-assignee: completed issues, cycle time, throughput, WIP |
| PO KPIs | Planning accuracy, scope change %, blockers, velocity, release bug quality |
| PROD Alignment | Epic progress against PROD items, customer-project tracking |
| Quality & Bugs | Bug counts by priority, reopen rates, QASE coverage, release bug history |

📖 See [docs/metrics-reference.md](docs/metrics-reference.md) for a full explanation of how every metric is calculated.

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
docker compose exec -T jira-sync python sync.py
```

Set `FULL_SYNC=1` in the environment to force a full re-sync of all issues regardless of last sync time.

### Subsequent syncs

Run automatically at 07:00 and 19:00 UTC. Trigger manually:

```bash
docker compose exec -T jira-sync python sync.py
```

## Custom fields synced

| Field | Jira ID | Column |
| --- | --- | --- |
| Story Points | `customfield_10016` (configurable) | `story_points` |
| Acceptance Criteria | `customfield_10028` (configurable) | `has_acceptance_criteria` (boolean) |
| Customer-Project | `customfield_10662` | `customer`, `project_name`, `customer_project` |
| QASE test case link | Synced separately via QASE API | `has_qase_link` (boolean, NULL = not yet checked) |

The Customer-Project field is a cascading select. Both parent (customer) and child (project) values are stored separately.

## Utilities

```bash
# List all Jira custom fields
docker compose exec -T jira-sync python list_custom_fields.py

# Filter by keyword
docker compose exec -T jira-sync python list_custom_fields.py customer

# Inspect raw value of a custom field on recent epics
docker compose exec -T jira-sync python debug_field.py customfield_10662
```

## Updating dashboards

Dashboard JSON files are in `grafana/provisioning/dashboards/` and are the single source of truth (`allowUiUpdates: false`). The save button in Grafana UI is disabled for provisioned dashboards.

To incorporate layout changes made in the Grafana UI:

1. In Grafana: Share → Export → Save to file
2. On your local machine, run the merge script (or let Claude Code merge it):
   - The exported JSON preserves your layout but has SQL from before your fixes
   - The merge applies correct SQL from the provisioned file to the exported layout
3. Overwrite `grafana/provisioning/dashboards/<dashboard>.json` with the merged file
4. Commit, push, and `git pull && docker compose restart grafana` on the server

## Sprint data model

### Tables

**`sprints`** — one row per Jira sprint, sourced from each board's sprint list.

**`sprint_issues`** — many-to-many between sprints and issues, with scope-change metadata:

| Column | Meaning |
| --- | --- |
| `was_in_initial_scope` | `TRUE` = committed at sprint start; `FALSE` = added mid-sprint |
| `removed_at` | `NULL` = still in sprint; timestamp = punted/removed mid-sprint |
| `story_points_at_add` | SP value at the moment the issue entered the sprint |

**`issue_fix_version_history`** — historical fix-version assignments per issue, populated from the Jira changelog. Unlike `issues.fix_versions` (current value only), this table tracks every release a bug was ever assigned to, including ones it was later moved away from. Used by the Release Bug History panels in Quality & Bugs.

| Column | Meaning |
| --- | --- |
| `issue_key` | Issue key |
| `fix_version` | Release/fix-version name |
| `added_at` | When this fix version was assigned (epoch sentinel if unknown) |
| `removed_at` | When it was removed; `NULL` if still assigned |

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

**`committed_points`** — `SUM(COALESCE(story_points_at_add, story_points, 0))` for issues where:

- `was_in_initial_scope = TRUE AND removed_at IS NULL`
- `issue_type NOT IN ('Epic', 'Sub-task')`
- `status != 'Obsolete / Won''t Do'`

**`delivered_points`** — same SUM, additionally filtered to `status_category = 'Done'`.

No `resolved_at` sprint-window filter is applied here. Because `was_in_initial_scope = TRUE` is set by the Jira sprint report (unique per sprint), there is no carry-over double-counting risk. A resolved_at window would incorrectly exclude issues resolved slightly after sprint close.

**`delivery_pct`** — `delivered_points / committed_points × 100`. Cannot exceed 100% because both numerator and denominator use only committed issues.

**Avg Velocity** in Sprint Detail takes `AVG(delivered_points)` over the 6 most recent closed sprints *before* the selected sprint's `start_date`, filtered by **project-majority**: a historical sprint is included only if >50% of its committed issues share a project key with the current sprint. No board_id filter is applied — some teams alternate between boards.

**Note:** The Sprint Detail **Completed SP** panel uses a different formula — it counts all Done issues (including unplanned) whose `resolved_at` falls within the sprint window `[start_date, COALESCE(complete_date, end_date, NOW())]`. This prevents carry-over double-counting for unplanned issues that appear in multiple sprints' `sprint_issues` with `removed_at IS NULL`.

### Known limitations

- **Active sprint scope tracking is heuristic**: `was_in_initial_scope` is inferred from sync timing for active sprints. Pass 2 corrects this for closed sprints using authoritative Jira sprint report data.
- **Cross-team boards**: if two teams share a Jira board, sprint velocity and sprint variable filters use project-majority (>50% of committed issues from the selected project) to separate teams. This works even when teams alternate board IDs.
- **fix_version history backfill**: the `issue_fix_version_history` table is populated incrementally. Issues not yet scanned are picked up on the next sync run. The first run after enabling this feature will scan all issues with fix_versions set (can take 60–90 minutes for large instances).

## Applying schema changes

After a `git pull` that includes `init.sql` changes, apply new columns and views (all statements are idempotent):

```bash
echo "$(cat init.sql)" | docker compose exec -T postgres psql -U metrics -d jira_metrics
```

Or for views only (faster):

```bash
docker compose exec -T postgres psql -U metrics -d jira_metrics -c "$(grep -A 100 'CREATE OR REPLACE VIEW v_planning_deviation' init.sql | head -60)"
```

No restart required for view-only changes.

## Database

Direct access:

```bash
docker compose exec -T postgres psql -U metrics -d jira_metrics
```

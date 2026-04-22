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

**`sprint_issues`** — many-to-many between sprints and issues, with live scope metadata. Queried directly by all dashboards.

| Column | Meaning |
| --- | --- |
| `was_in_initial_scope` | `TRUE` = committed at sprint start; `FALSE` = added mid-sprint. Set authoritatively by `sync_sprint_scope` from changelog history. |
| `removed_at` | `NULL` = still in sprint; timestamp = punted/removed. Set from `issue_sprint_history` for punted issues. |
| `story_points_at_add` | SP value when the issue entered the sprint |

**`issue_sprint_history`** — append-only log of Sprint field changes from each issue's Jira changelog. Every time a sprint ID is added to or removed from an issue, one row is inserted. This is the authoritative source for determining when issues entered or left a sprint.

| Column | Meaning |
| --- | --- |
| `sprint_id` | Jira sprint ID (no FK — may reference sprints on other boards) |
| `event` | `'added'` or `'removed'` |
| `occurred_at` | Timestamp from the Jira changelog entry |

**`sprint_scope_initial`** — snapshot of which issues were committed at sprint start, derived from `issue_sprint_history` (first `'added'` event ≤ `sprint.start_date + 2h`). Rebuilt each sync for active sprints; set once when a sprint closes.

**`sprint_scope_final`** — per-issue final state for closed sprints, derived from `sprint_issues` membership and `issues.resolved_at`.

| Column | Meaning |
| --- | --- |
| `was_completed` | `TRUE` if `status_category = 'Done'` and `resolved_at` within sprint window |
| `was_punted` | `TRUE` if a `'removed'` event exists in `issue_sprint_history` |
| `was_added_mid_sprint` | `TRUE` if first `'added'` event was after `sprint.start_date + 2h` |

**`sprint_scope_changes`** — audit log of additions and removals detected between active-sprint sync runs (±12 hour precision). Populated by `_sync_sprint_members`; useful for auditing mid-sprint changes.

**`issue_fix_version_history`** — historical fix-version assignments per issue, populated from the Jira changelog. Unlike `issues.fix_versions` (current value only), this table tracks every release a bug was ever assigned to. Used by the Release Bug History panels in Quality & Bugs.

| Column | Meaning |
| --- | --- |
| `fix_version` | Release/fix-version name |
| `added_at` | When this fix version was assigned (epoch sentinel if unknown) |
| `removed_at` | When it was removed; `NULL` if still assigned |

### How sprint membership is populated

**Step 1 — live membership** (`_sync_sprint_members`, every sync):
Calls `/agile/1.0/sprint/{id}/issue` and upserts current members into `sprint_issues`. Also detects additions and removals since the last sync and writes them to `sprint_scope_changes`.

**Step 2 — sprint history backfill** (`backfill_sprint_history`, every sync, incremental):
Fetches Jira changelogs for any sprint-issues not yet in `issue_sprint_history`. Runs in O(n) on first deployment, then only processes new issues. Populates `issue_sprint_history` with every sprint add/remove event ever recorded in Jira.

**Step 3 — scope derivation** (`sync_sprint_scope`, every sync):
Reads `issue_sprint_history` to determine, per sprint:

- `was_in_initial_scope`: first `'added'` event ≤ `sprint.start_date + 2h` → `TRUE`
- `was_punted`: any `'removed'` event exists for this sprint
- `was_added_mid_sprint`: first `'added'` event > `sprint.start_date + 2h`

Updates `sprint_issues.was_in_initial_scope` and `sprint_issues.removed_at` accordingly, then populates `sprint_scope_initial` (all sprints) and `sprint_scope_final` (closed sprints only). Sets `scope_synced_at` on the sprint row — closed sprints are not reprocessed after that.

If `issue_sprint_history` has no entries for a sprint (history not yet backfilled), the function falls back to treating all current `sprint_issues` members as initial scope so dashboards remain populated during the transition.

### How velocity and delivery are calculated

`v_planning_deviation` computes per-sprint:

**For closed sprints** (using `sprint_scope_final`):

- **`committed_points`** — `SUM(story_points)` for issues where `was_punted = FALSE`, excl. Epics/Sub-tasks/Obsolete
- **`delivered_points`** — `SUM(story_points)` for issues where `was_completed = TRUE`, excl. Epics/Sub-tasks/Obsolete
- No `resolved_at` window needed — `was_completed` is derived from the issue's resolved state at sprint close and is unique per sprint, so there is no carry-over double-counting risk.

**For active/future sprints** (falling back to `sprint_issues`):

- **`committed_points`** — `SUM(COALESCE(story_points_at_add, story_points, 0))` where `was_in_initial_scope = TRUE AND removed_at IS NULL`
- **`delivered_points`** — same SUM, additionally filtered to `status_category = 'Done'` with a 7-day `resolved_at` window after sprint close to prevent carry-over double-counting

**`delivery_pct`** — `delivered_points / committed_points × 100`. Cannot exceed 100% because only committed issues are in both numerator and denominator.

**Avg Velocity** in Sprint Detail takes `AVG(delivered_points)` over the 6 most recent closed sprints *before* the selected sprint's `start_date`, filtered by **project-majority**: a sprint is included only if >50% of its committed issues share a project key with the current sprint. No board_id filter — some teams alternate between boards.

**Note:** The Sprint Detail **Completed SP** stat panel uses a different formula — it counts all Done issues (including unplanned) whose `resolved_at` falls within the sprint window `[start_date, COALESCE(complete_date, end_date, NOW())]`.

### Known limitations

- **Mid-sprint change precision**: `sprint_scope_changes` captures additions/removals with ±12 hour precision (sync cadence). Jira's changelog provides exact timestamps in `issue_sprint_history`, which is used for initial-scope determination. Short-lived changes (added and removed within one 12-hour sync window) may not appear in `sprint_scope_changes`.
- **Issues punted before first sync**: if an issue was added to and removed from a sprint entirely before the first sync ran, it won't appear in `sprint_issues` and `backfill_sprint_history` won't scan it. Its `issue_sprint_history` entries are captured when `sync_issues` processes that issue's changelog, but it won't be reflected in `sprint_scope_final`.
- **Cross-team boards**: if two teams share a Jira board, sprint velocity and sprint variable filters use project-majority (>50% of committed issues from the selected project) to separate teams.
- **sprint_history backfill**: on first deployment, `backfill_sprint_history` fetches changelogs for all issues in tracked sprints. Expect 5–30 minutes depending on issue count. Subsequent runs are incremental.
- **fix_version history backfill**: same incremental pattern. First run can take 60–90 minutes for large instances.

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

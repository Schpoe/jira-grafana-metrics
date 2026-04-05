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
| Sprint Detail | Per-sprint breakdown: story points, burndown, scope changes, readiness, QASE coverage, data quality |
| Sprint Health | Cross-sprint velocity, delivery %, scope change trends |
| Flow & Cycle Time | Lead time, cycle time, throughput |
| Team Overview | Per-team summary across sprints |
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

## Database

Direct access:

```bash
docker compose exec postgres psql -U metrics -d jira_metrics
```

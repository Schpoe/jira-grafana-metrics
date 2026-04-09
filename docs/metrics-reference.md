# Metrics Reference — All Dashboards

This document explains how every metric across all dashboards is calculated, which filters are applied, and known differences between dashboards.

**Data source:** PostgreSQL (`jira-metrics-pg`).  
**Key tables:** `sprint_issues`, `issues`, `sprints`, `issue_transitions`, `issue_links`, `releases`.  
**Key views:** `v_planning_deviation`, `v_cycle_time_rft_to_done`, `v_cycle_time_in_progress_to_rft`, `v_lead_time`, `v_time_in_status`, `v_prod_epic_progress`, `v_prod_item_progress`.

---

## Known Cross-Dashboard Differences

These are intentional differences worth understanding when comparing numbers:

| Topic | Sprint Detail / PO KPIs / Sprint Overview | Team Overview | Quality & Bugs |
|-------|------------------------------------------|---------------|----------------|
| **Quarter assignment** | `start_date` | `resolved_at` | `created_at` (bugs), `complete_date` (QASE) |
| **story_points field** | `story_points_at_add` (scope-aware) | `story_points` (current) | N/A |
| **Completed SP** | All Done issues with `resolved_at` window | N/A | N/A |
| **Obsolete exclusion** | Yes | Yes (fixed) | No |
| **QASE Sub-task exclusion** | Yes | N/A | Yes (fixed) |

**Quarter assignment:** All dashboards now use `start_date` to assign sprints to quarters. A sprint belongs to the quarter it started in, regardless of when it closed.

---

## `v_planning_deviation` View

Used by: Sprint Detail (Delivery %, Avg Velocity, Velocity Booked %), Sprint Overview, PO KPIs.

### `committed` CTE
```sql
WHERE was_in_initial_scope = TRUE
  AND removed_at IS NULL
  AND issue_type NOT IN ('Epic', 'Sub-task')
  AND status != 'Obsolete / Won''t Do'
```
- `committed_points`: `SUM(COALESCE(story_points_at_add, story_points, 0))`
- `committed_issues`: `COUNT(*)`

### `delivered` CTE
```sql
WHERE was_in_initial_scope = TRUE
  AND removed_at IS NULL
  AND status_category = 'Done'
  AND status != 'Obsolete / Won''t Do'
  AND issue_type NOT IN ('Epic', 'Sub-task')
```
- `delivered_points`: `SUM(COALESCE(story_points_at_add, story_points, 0))`
- `delivered_issues`: `COUNT(*)`

**Why no `resolved_at` window:** `was_in_initial_scope = TRUE` is set by the Jira sprint report API at sprint start only — unique per sprint. There is no carry-over double-counting risk. A `resolved_at` window would incorrectly exclude issues resolved slightly outside the sprint window (e.g. a few hours after close).

**`delivery_pct`:** `ROUND(100.0 * delivered_points / committed_points, 1)` — NULL when committed = 0. Cannot exceed 100% because only committed issues are in the numerator.

**Note:** The Sprint Detail **Completed SP** panel uses a different formula — it counts *all* Done issues (including unplanned) and does use a `resolved_at` sprint window to prevent carry-over double-counting.

---

## Sprint Detail Dashboard

Per-sprint drill-down for Scrum Masters and Release Managers.

**Common exclusions (unless noted):** `issue_type IN ('Epic', 'Sub-task')`, `status = 'Obsolete / Won''t Do'`, `removed_at IS NOT NULL`.

### Story Points

**Committed** — `SUM(COALESCE(story_points_at_add, story_points, 0))` where `was_in_initial_scope = TRUE AND removed_at IS NULL`, excl. Epics, Sub-tasks, Obsolete. Uses `story_points_at_add` so mid-sprint re-estimation doesn't change what was committed.

**Unplanned** — Same SUM for `was_in_initial_scope = FALSE AND removed_at IS NULL`. Target ≤ 10% of Committed.

**Total in Sprint** — SUM where `removed_at IS NULL`, excl. Epics, Sub-tasks, Obsolete.

**Obsolete / Won't Do** — SUM where `removed_at IS NULL AND status = 'Obsolete / Won''t Do'`. No type filter — counts all types so nothing is silently hidden.

**Completed** — SUM where `status_category = 'Done'`, excl. Epics/Sub-tasks/Obsolete, with `resolved_at` in sprint window (`>= start_date AND <= COALESCE(complete_date, end_date, NOW())`). Counts all Done issues including unplanned. The `resolved_at` window is required here because unplanned Done issues appear in multiple sprints' `sprint_issues` with `removed_at IS NULL`.

**Delivery %** — `delivery_pct` from `v_planning_deviation`. Gauge: red < 60%, yellow 60–80%, green ≥ 80%.

**Avg Velocity (Last 6 Sprints)** — `AVG(delivered_points)` from `v_planning_deviation` across the 6 most recent closed sprints (by `complete_date`) before the current sprint's `start_date`, filtered to the same team via project-majority filter (>50% of committed issues share a project key). No board_id filter — some teams alternate boards.

**Velocity Booked %** — `ROUND(100.0 * committed_sp / avg_velocity, 1)`. Gauge: green < 80%, yellow 80–100%, red > 100% (capped at 150%).

### Story Readiness

**Planned** — `COUNT(*)` where `was_in_initial_scope = TRUE AND issue_type NOT IN ('Epic','Sub-task')`. No `removed_at` filter. Bugs included.

**Ready Issues** — Issues with SP > 0 AND epic_key IS NOT NULL AND `has_acceptance_criteria = TRUE`. All three must be met. Excl. Epics, Sub-tasks, removed.

**Missing Assignee** — `COUNT(*)` where `removed_at IS NULL AND assignee IS NULL AND status != 'Obsolete/Won''t Do'`. All issue types including Epics and Sub-tasks.

**Missing AC** — Issues where `has_acceptance_criteria IS NULL OR FALSE`, excl. Epics, Sub-tasks, **Bugs** (Bugs don't require AC).

**Missing SP** — Issues where `story_points IS NULL OR story_points = 0`, excl. Epics, Sub-tasks.

**Missing Epic Link** — Issues where `epic_key IS NULL`, excl. Epics, Sub-tasks.

**Readiness %** — `ROUND(100.0 * ready / total, 1)` where ready = SP>0 AND epic IS NOT NULL AND has_ac = TRUE, total = all active non-Epic/Sub-task issues. Bugs count in denominator even without AC requirement. Target ≥ 90%.

**Completed Issues** — `COUNT(*)` with same `resolved_at` sprint window as Completed SP. Excl. Epics, Sub-tasks, Obsolete.

**Open Issues** — `COUNT(*)` where `removed_at IS NULL AND status_category != 'Done'`, excl. Epics, Sub-tasks, Obsolete.

**Issues Not Ready (table)** — Issues failing any of: SP missing, Epic missing, AC missing (N/A for Bugs), Assignee missing. Bugs only appear for SP/Epic/Assignee — not AC. Sorted by priority.

### Scope Change

**Issues Added / SP Added** — `was_in_initial_scope = FALSE AND removed_at IS NULL`. No type filter.

**Issues Removed / SP Removed** — `removed_at IS NOT NULL`. No type filter.

**Scope Change %** — `(SP_added + SP_removed) / committed_SP × 100`. The committed denominator has no type filter (raw sprint data). Target ≤ 10%, orange at 10%, red at 25%.

### Cross-Team Dependencies

Uses `issue_links` table with `link_label = 'blocks'` / `'is blocked by'`. Only cross-project links (`i2.project_key != i.project_key`).

**Blocking Other Teams** / **Blocked by Other Teams** — Stat counts. Thresholds: green = 0, yellow ≥ 1, red ≥ 3.

**Detail tables** — Key, Summary, Status, Priority, Assignee, linked issue key (Jira link), Other Team, linked issue summary. Sorted by priority.

### Issue Counts — Summary Table

Single-row overview. The **Completed** column here uses `removed_at IS NULL AND status_category = 'Done'` without the `resolved_at` window. This reflects live Done status and may include carry-over issues. Use the Completed SP and Completed Issues stat panels for authoritative counts.

### All Issues in Sprint

**Times Carried** — Count of prior sprints the issue appeared in with `removed_at IS NULL AND sprint_id < current_sprint`. Orange ≥ 1, red ≥ 3.

### QASE Test Coverage (collapsed)

Filters to `removed_at IS NULL AND issue_type != 'Epic'`. Sub-tasks are included.

| Panel | Logic |
|-------|-------|
| With QASE Link | `has_qase_link = TRUE` |
| Without QASE Link | `has_qase_link = FALSE` |
| Not Yet Checked | `has_qase_link IS NULL` |
| QASE Coverage % | `COUNT(TRUE) / COUNT(NOT NULL)` — excludes unchecked from denominator |

### Burndown Chart

1. Committed issues: `was_in_initial_scope = TRUE`.
2. Total SP = sum of committed SP.
3. Done per issue: earliest `issue_transitions.transitioned_at` where `UPPER(to_status) = 'DONE'` and `transitioned_at >= sprint.start_date`.
4. Remaining SP: `total_sp − SUM(sp for issues Done before end of day)`. NULL for future days.
5. Ideal line: `total_sp × (1 − elapsed_fraction)`, floored at 0.

---

## Sprint Overview Quarter Dashboard

Quarter-level view of sprint velocity and delivery across all team sprints.

**Quarter assignment:** Uses `COALESCE(complete_date, start_date)` — sprints appear in the quarter they completed (or started, for active sprints). This differs from Sprint Detail which uses `start_date`.

**Sprint filter:** Simple `project_key IN ($project)` join — shows sprints that have at least one issue from the selected project.

All SP metrics flow through `v_planning_deviation`.

| Panel | Source | Notes |
|-------|--------|-------|
| Sprint Velocity — Committed vs Delivered | `v_planning_deviation` | committed_points, delivered_points |
| Delivery % per Sprint | `v_planning_deviation` | `delivery_pct`, green ≥ 90%, yellow ≥ 70%, red < 70% |
| Sprint Issue Count — Committed vs Delivered | `v_planning_deviation` | committed_issues, delivered_issues |
| Sprint Summary Table | `v_planning_deviation` | Includes Deviation = delivered − committed |

---

## PO KPIs Dashboard

Quarterly KPIs for Product Owners: planning quality, delivery accuracy, re-work, blockers, release quality.

**Quarter assignment:** Uses `start_date` (sprints assigned to the quarter they started in). Consistent with Sprint Detail, differs from Sprint Overview Quarter.

**Sprint filter:** Project-majority filter (`was_in_initial_scope = TRUE`, >50% committed issues from selected projects). More accurate than simple join for multi-team boards.

### Planning & Delivery

**Avg Scope Change %** — `AVG((added_sp + removed_sp) / committed_sp × 100)` across closed sprints in quarter. Target ≤ 10%.

**Avg Planning Accuracy %** — `AVG(delivery_pct)` from `v_planning_deviation` for closed sprints. Since `delivery_pct` only counts committed issues, this cannot exceed 100%.

**Ticket Reopens** — Issues that transitioned from a testing status to In Progress: `LOWER(from_status) LIKE '%test%' AND LOWER(to_status) LIKE '%progress%'`. Filtered by `transitioned_at` quarter.

**Avg Blocker Resolution Time** — `AVG((resolved_at - created_at) / 3600)` in hours for Blocker issues created in the quarter. Uses `created_at` for quarter assignment so it measures blockers that originated in the quarter, regardless of when resolved.

**Scope Change % per Sprint** — Per-sprint bar chart of `(added_sp + removed_sp) / committed_sp`. Ordered by `start_date ASC`.

**Planning Accuracy % per Sprint** — Shows Committed SP, Delivered SP, and Delivery % per sprint from `v_planning_deviation`. Ordered oldest to newest.

**Sprint Velocity — Committed vs Delivered** — Bar chart from `v_planning_deviation`.

**Blocker Resolution Time per Sprint** — `AVG((resolved_at - created_at) / 3600)` per sprint for Blocker issues. Uses `was_in_initial_scope = TRUE` to avoid carry-over double-counting.

**Open Blockers** — Currently open (`status_category != 'Done'`) Blocker issues, ordered by age. No time filter — shows all open blockers regardless of quarter.

**Weekly Ticket Reopen Rate** — Time-series using Grafana `$__timeFilter`. Spike = quality regression.

**Most Reopened Issues** — Issues with most reopen transitions in selected quarter.

### Release Bug Quality

Links `issues.fix_versions` (array) to `releases` via `r.name = ANY(i.fix_versions) AND r.project_key = i.project_key`. Quarter filtered by `release_date`.

**Major Bugs in Releases** — Count of Blocker+Critical+High bugs in released versions. Thresholds: green < 10, yellow 10–30, orange 30–60, red ≥ 60.

**Blocker Bugs in Releases** — Blocker-only count. Thresholds: green < 3, yellow 3–10, orange 10–20, red ≥ 20.

**Total Bugs in Releases** — All priorities combined.

**Bugs per Release (bar chart)** — Stacked by priority (dark-red=Blocker, red=Critical, orange=High, yellow=Medium, green=Low), ordered by `release_date ASC`.

**Major Bug Details** — Table of Blocker/Critical/High bugs with release name, date, priority, status, assignee, Jira link.

---

## Team Overview Dashboard

Per-assignee metrics for selected team and quarter/sprint.

**Sprint vs Quarter mode:** All panels support both. When `$sprint = 0` (All), filters by `date_trunc('quarter', resolved_at) = '$quarter'`. When a sprint is selected, filters by `key IN (SELECT issue_key FROM sprint_issues WHERE sprint_id = $sprint AND removed_at IS NULL)`.

**Note — story points field:** This dashboard uses `issues.story_points` (current value) rather than `sprint_issues.story_points_at_add`. This means if an issue's estimate changed after sprint start, the current value is used. This is intentional for the WIP display (current workload) but means delivery SP may differ slightly from Sprint Detail.

**Note — Obsolete exclusion:** Issues marked "Obsolete / Won't Do" have `status_category = 'Done'` and will be counted in completed issue counts. This is a known inconsistency with Sprint Detail.

| Panel | What it measures |
|-------|-----------------|
| Current WIP per Assignee | Open `In Progress` issues, current `story_points` |
| Avg Cycle Time (RFT→Done) per Assignee | From `v_cycle_time_rft_to_done`, avg `hours_rft_to_done` |
| Issues Completed per Assignee | `status_category = 'Done'` count + SP, filtered by quarter or sprint |
| Where Issues Spend Most Time | From `v_time_in_status`, avg hours per status, ≥ 5 issues, < 8760h (1yr cap) |
| Throughput Trend (weekly) | `DATE_TRUNC('week', resolved_at)` count per assignee |
| Assignee Detail Table | Summary: issues completed, SP, bugs fixed, avg cycle time, current WIP |

---

## Flow & Cycle Time Dashboard

Time-based metrics for process flow analysis. Uses Grafana `$__timeFilter` (time picker) for most panels rather than the quarter variable.

**Quarter variable** is used for cycle time breakdowns (by type, by priority) but the main trend panels use the time picker. This means the time scope may differ from other dashboards when comparing quarterly numbers.

**Cycle time views** (`v_cycle_time_rft_to_done`, `v_cycle_time_in_progress_to_rft`) exclude Epics and Sub-tasks. No explicit Obsolete exclusion.

### Cycle Time: RFT → Done

Time from the first transition into a "Ready For Testing" status to `resolved_at`. Measures testing and review speed.

| Panel | Logic |
|-------|-------|
| Weekly trend | Avg, Median, p85 per week by `resolved_at` |
| By Issue Type | Avg per type, quarter filter |
| By Priority | Avg per priority, ordered Blocker→Low |
| Slowest Issues | Top 25 by `hours_rft_to_done` |

### Cycle Time: In Progress → RFT

Time from first "In Progress" transition to first "Ready For Testing" entry. Measures development speed.

Same panel structure as RFT→Done, filtered by `entered_rft_at` date.

### Weekly Throughput

`COUNT(*)` of issues with `status_category = 'Done'` per week, filtered by `resolved_at` quarter. No type or Obsolete filter.

### Current WIP by Status

Open `In Progress` issues grouped by status. SUM of `story_points` (current). No time filter — always shows current live state.

### Lead Time

`v_lead_time` view: days from `created_at` to `resolved_at`. Uses `$__timeFilter` on `resolved_at`.

| Panel | Logic |
|-------|-------|
| Avg Lead Time | `AVG(days_lead_time)` |
| Median Lead Time | `PERCENTILE_CONT(0.5)` |
| Lead Time Trend | Weekly avg |
| By Issue Type / Priority | Avg per group |
| Slowest Issues | Top 50 |

---

## Quality & Bugs Dashboard

Bug tracking and QASE test coverage. Has an additional `$release` (Fix Version) variable.

**Release filter:** `$release = 'ALL' OR $release = ANY(fix_versions)` — when a release is selected, only bugs linked to that fix version are shown.

**Quarter assignment for QASE panels:** Uses `COALESCE(s.complete_date, s.start_date)` (sprint completion quarter). This differs from Sprint Detail which uses `start_date`.

**QASE panels exclude Epics only** (not Sub-tasks). This differs from Sprint Detail which excludes both.

### Bug Metrics

| Panel | What it counts | Priority filter |
|-------|----------------|----------------|
| Open Critical Bugs | `status_category != 'Done' AND priority = 'Blocker'` | Blocker |
| Open High Bugs | `status_category != 'Done' AND priority = 'Critical'` | Critical |
| Open Medium Bugs | `status_category != 'Done' AND priority = 'High'` | High |
| Open Low Bugs | `status_category != 'Done' AND priority IN ('Medium','Low')` | Medium+Low |

**Note:** The panel titles say Critical/High/Medium/Low but the SQL filters Blocker/Critical/High/Medium+Low respectively. The labels are one level off from the data.

**Bug Creation Rate** — Weekly `COUNT(*)` by priority using `$__timeFilter(created_at)`.

**Bugs per Release** — `UNNEST(fix_versions)` join, filtered by `created_at` quarter. Shows Total, Resolved, Open per release.

**Bug Resolution Rate vs Creation Rate** — Two time-series: `COUNT(*)` by `created_at` vs `resolved_at` per week.

**Open Bugs — Critical & High** — `status_category != 'Done' AND priority IN ('Blocker','Critical')`, ordered by age ASC (oldest first).

### QASE Coverage (Quality & Bugs)

All panels filter to `issue_type != 'Epic'` (Sub-tasks included) and use sprint-quarter linkage via `key IN (SELECT DISTINCT si.issue_key FROM sprint_issues si JOIN sprints s ... WHERE date_trunc('quarter', COALESCE(s.complete_date, s.start_date)) = '$quarter'::date)`.

| Panel | Logic |
|-------|-------|
| With QASE Link | `has_qase_link = TRUE` |
| Without QASE Link | `has_qase_link = FALSE` |
| QASE Coverage % | `COUNT(TRUE) / COUNT(NOT NULL)` |
| Not Yet Checked | `has_qase_link IS NULL` |
| By Issue Type (bar) | With/Without counts per `issue_type` |
| By Issue Type (table) | Total, With, Without, Not Checked, Coverage % per type |
| By Issue Status (table) | Same breakdown per `status` |

---

## PROD Alignment Dashboard

Tracks delivery against PROD-level items via Epic→PROD issue links.

**Quarter variable:** Derived from `issues.created_at WHERE issue_type = 'Epic'` — shows quarters in which Epics were created. This means it filters by when Epics were written, not by when they were delivered.

**PROD link:** Epics link to PROD items via `issue_links` where `to_key LIKE 'PROD-%' AND link_label = 'implements'`. Deduplication handled in views (`v_prod_epic_progress`, `v_prod_item_progress`).

**Completion %** is calculated at two levels:
- **Epic level** (`v_prod_epic_progress`): Done child issues / total child issues × 100
- **PROD level** (`v_prod_item_progress`): Aggregated across all Epics linked to the PROD item

Child issues exclude Epics and Sub-tasks.

| Panel | What it measures |
|-------|-----------------|
| PROD Items Linked | Distinct PROD keys referenced by Epics in project+quarter |
| Epics Linked to PROD | Count of Epics with at least one implements link |
| Epics Without PROD Link | Epics with no `PROD-%` link target, in project+quarter |
| Avg Completion % | `AVG(completion_pct_issues)` from `v_prod_item_progress` |
| All PROD Items table | Per-PROD: epic count, issues, SP, done count, completion % (SP-based) |
| Epic Breakdown table | Per-Epic: PROD key, status, issues, SP, completion % |
| Epics Without PROD Link table | No quarter filter — shows all unlinked Epics for the project |

**Note:** The "Epics Without PROD Link" table has no quarter filter and shows all historical unlinked Epics. This is intentional (backlog visibility).

---

## Variable Filters (all dashboards)

| Variable | Source | Notes |
|----------|--------|-------|
| `$jira_url` | `app_settings` table | Hidden variable used for Jira deep-links |
| `$project` | `projects.key` | Multi-select, drives all team/project filters |
| `$quarter` | `issues.created_at` distinct quarters | Descending order; used as `date_trunc('quarter', ...) = '$quarter'::date` |
| `$sprint` | `sprints` table | Quarter + project-majority filtered; 0 = All |
| `$release` | `UNNEST(issues.fix_versions)` | Quality & Bugs only; ALL = no release filter |
| `$prod_item` | `issue_links.to_key LIKE 'PROD-%'` | PROD Alignment only; ALL = all items |

**Sprint variable project-majority filter:** A sprint is included in the dropdown only if more than 50% of its `was_in_initial_scope = TRUE` issues belong to the selected project(s). This prevents cross-team sprints appearing when a few issues from another team are in the sprint.

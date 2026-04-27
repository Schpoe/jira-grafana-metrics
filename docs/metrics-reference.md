# Metrics Reference — All Dashboards

This document explains how every metric across all dashboards is calculated, which filters are applied, and known differences between dashboards.

**Data source:** PostgreSQL (`jira-metrics-pg`).  
**Key tables:** `sprint_issues`, `issues`, `sprints`, `issue_transitions`, `issue_links`, `releases`, `issue_fix_version_history`, `issue_sprint_history`, `sprint_scope_initial`, `sprint_scope_final`, `sprint_scope_changes`.  
**Key columns added:** `issues.qa_assignee TEXT` (Jira field `customfield_10132` — QA person assigned), `issues.components TEXT[]` (standard Jira components array).  
**Key views:** `v_planning_deviation`, `v_cycle_time_rft_to_done`, `v_cycle_time_in_progress_to_rft`, `v_lead_time`, `v_time_in_status`, `v_prod_epic_progress`, `v_prod_item_progress`.

---

## Known Cross-Dashboard Differences

These are intentional differences worth understanding when comparing numbers:

| Topic | Sprint Detail / PO KPIs / Sprint Overview | Team Overview | Quality & Bugs |
|-------|------------------------------------------|---------------|----------------|
| **Quarter assignment** | `start_date` | `resolved_at` (quarter mode) | `created_at` (bugs), `COALESCE(complete_date, start_date)` (QASE) |
| **story_points field** | `story_points_at_add` (scope-aware) | `story_points` (current) | N/A |
| **Completed SP** | All Done issues with `resolved_at` window (Sprint Detail stat panels) | N/A | N/A |
| **Obsolete exclusion** | Yes | Yes | Yes (bug stats and QASE) |
| **QASE issue exclusion** | Epics, Sub-tasks, Open status, Obsolete status | N/A | Epics, Sub-tasks, Open status, Obsolete status |

**Quarter assignment:** Sprint Detail, PO KPIs, and Sprint Overview Quarter all use `start_date` to assign sprints to quarters. A sprint belongs to the quarter it started in, regardless of when it closed. Quality & Bugs QASE panels use `COALESCE(complete_date, start_date)` for sprint-quarter linkage.

---

## `v_planning_deviation` View

Used by: Sprint Detail (Delivery %, Avg Velocity, Velocity Booked %), Sprint Overview Quarter, PO KPIs.

The view uses two different data sources depending on whether a sprint has been processed into the scope tables:

**For closed sprints** (where `sprint_scope_final` rows exist):

### `committed` CTE (closed sprints)

```sql
FROM sprint_scope_final ssf
WHERE ssf.was_punted = FALSE
  AND issue_type NOT IN ('Epic', 'Sub-task')
  AND status != 'Obsolete / Won''t Do'
```

- `committed_points`: `SUM(COALESCE(ssf.story_points, 0))`
- `committed_issues`: `COUNT(*)`

### `delivered` CTE (closed sprints)

```sql
FROM sprint_scope_final ssf
WHERE ssf.was_completed = TRUE
  AND issue_type NOT IN ('Epic', 'Sub-task')
  AND status != 'Obsolete / Won''t Do'
```

- `delivered_points`: `SUM(COALESCE(ssf.story_points, 0))`
- `delivered_issues`: `COUNT(*)`

**Why no `resolved_at` window for closed sprints:** `was_completed` is derived from issue changelog history and stored in `sprint_scope_final` at sprint close time. It is unique per sprint — no carry-over double-counting risk.

**For active/future sprints** (falling back to `sprint_issues`):

### `committed` CTE (active sprints)

```sql
WHERE was_in_initial_scope = TRUE
  AND removed_at IS NULL
  AND issue_type NOT IN ('Epic', 'Sub-task')
  AND status != 'Obsolete / Won''t Do'
```

- `committed_points`: `SUM(COALESCE(story_points_at_add, story_points, 0))`

### `delivered` CTE (active sprints)

Same filter plus `status_category = 'Done'` and `resolved_at <= COALESCE(complete_date, end_date, NOW()) + INTERVAL '7 days'`. The `resolved_at` window prevents carry-over double-counting for active sprints where `sprint_scope_final` is not yet available.

---

**`delivery_pct`:** `ROUND(100.0 * delivered_points / committed_points, 1)` — NULL when committed = 0. Cannot exceed 100% because only committed issues are in the numerator.

**How `was_in_initial_scope` and `was_completed` are set:** `sync_sprint_scope()` reads `issue_sprint_history` (changelog-derived sprint field changes) and compares event timestamps to `sprint.start_date`. Issues with a first `'added'` event ≤ `start_date + 2h` are initial scope. `was_completed` is set from `issues.status_category` and `resolved_at` at sprint close time.

**Note:** The Sprint Detail **Completed SP** stat panel uses a different formula — it counts *all* Done issues (including unplanned) and does use a `resolved_at` sprint window (`>= start_date AND <= COALESCE(complete_date, end_date, NOW())`) to prevent carry-over double-counting.

---

## `issue_fix_version_history` Table

Tracks every `fix_version` ever assigned to an issue via the Jira changelog, even if the issue was later moved to a different release.

| Column | Description |
|--------|-------------|
| `issue_key` | Issue key |
| `fix_version` | Fix version name |
| `added_at` | When this fix version was assigned (defaults to epoch if not known) |
| `removed_at` | When this fix version was removed; NULL if still assigned |

Used by the Quality & Bugs dashboard **Release Bug History** section to identify bugs that were ever scoped into a release, including those subsequently moved to a different fix version.

---

## Sprint Detail Dashboard

Per-sprint drill-down for Scrum Masters and Release Managers.

**Common exclusions (unless noted):** `issue_type IN ('Epic', 'Sub-task')`, `status = 'Obsolete / Won''t Do'`, `removed_at IS NOT NULL`.

### Story Points

**Committed** — `SUM(COALESCE(story_points_at_add, story_points, 0))` where `was_in_initial_scope = TRUE AND removed_at IS NULL`, excl. Epics. Sub-tasks and Obsolete are included. Uses `story_points_at_add` so mid-sprint re-estimation doesn't change what was committed.

**Unplanned** — Same SUM for `was_in_initial_scope = FALSE AND removed_at IS NULL`, excl. Epics. Sub-tasks included. Target ≤ 10% of Committed.

**Total in Sprint** — SUM where `removed_at IS NULL`, excl. Epics. Sub-tasks included.

**Obsolete / Won't Do** — SUM where `removed_at IS NULL AND status = 'Obsolete / Won''t Do'`. No type filter — counts all types so nothing is silently hidden.

**Completed** — SUM where `status_category = 'Done'`, excl. Epics, with `resolved_at` in sprint window (`>= start_date AND <= COALESCE(complete_date, end_date, NOW())`). `resolved_at IS NOT NULL` required. Counts all Done issues including unplanned and Sub-tasks. The `resolved_at` window is required here because unplanned Done issues appear in multiple sprints' `sprint_issues` with `removed_at IS NULL`.

**Delivery %** — `delivery_pct` from `v_planning_deviation`. Gauge: red < 60%, yellow 60–80%, green ≥ 80%.

**Avg Velocity (Last 6 Sprints)** — `AVG(delivered_points)` from `v_planning_deviation` across the 6 most recent closed sprints (by `complete_date`) before the current sprint's `start_date`, filtered to the same team via project-majority filter (>50% of committed issues share a project key). No board_id filter — some teams alternate boards.

**Velocity Booked %** — `ROUND(100.0 * committed_sp / avg_velocity, 1)`. Gauge: green < 80%, yellow 80–100%, red > 100% (capped at 150%).

### Story Readiness

**Planned** — `COUNT(*)` where `was_in_initial_scope = TRUE AND issue_type != 'Epic'`. No `removed_at` filter. Sub-tasks and Bugs included.

**Ready Issues** — Stories with SP ≥ 1 AND epic_key IS NOT NULL AND `has_acceptance_criteria = TRUE` AND `cardinality(components) > 0`. All four must be met. Excl. removed, Obsolete.

**Missing Assignee** — `COUNT(*)` Stories where `removed_at IS NULL AND assignee IS NULL AND status != 'Obsolete / Won''t Do'`. Stories only.

**Missing AC** — Stories where `has_acceptance_criteria IS NULL OR FALSE`, excl. Obsolete. Stories only (Bugs, Tasks not shown).

**Missing SP** — Stories where `story_points IS NULL OR story_points = 0`, excl. Obsolete. Stories only.

**Missing Epic Link** — Stories where `epic_key IS NULL`, excl. Obsolete. Stories only.

**Readiness %** — `ROUND(100.0 * ready / total, 1)` where ready = SP≥1 AND epic IS NOT NULL AND has_ac = TRUE AND cardinality(components) > 0, total = all active Stories in sprint (excl. removed). Target ≥ 90%.

**Completed Issues** — `COUNT(*)` with same `resolved_at` sprint window as Completed SP. Excl. Epics and Obsolete. Sub-tasks included.

**Open Issues** — `COUNT(*)` where `removed_at IS NULL AND status_category != 'Done'`, excl. Epics, Obsolete. Sub-tasks included.

**Issues Not Ready (table)** — Stories failing any of: SP missing, Epic missing, AC missing, Assignee missing, Component missing. Stories only. Sorted by priority. Includes a Component ✅/❌ column.

### Scope Change

**Issues Added / SP Added** — `was_in_initial_scope = FALSE AND removed_at IS NULL`. No type filter.

**Issues Removed / SP Removed** — `removed_at IS NOT NULL`. No type filter.

**Scope Change %** — `(SP_added + SP_removed) / committed_SP × 100`. Epics excluded from all three terms; Sub-tasks and other types included. Target ≤ 10%, orange at 10%, red at 25%.

### Cross-Team Dependencies

Uses `issue_links` table with `link_label = 'blocks'` / `'is blocked by'`. Only cross-project links (`i2.project_key != i.project_key`).

**Blocking Other Teams** / **Blocked by Other Teams** — Count of cross-team blocking *relationships* (not distinct issues). One sprint issue blocking two external tickets counts as 2. Thresholds: green = 0, yellow ≥ 1, red ≥ 3.

**Detail tables** — Key, Summary, Status, Priority, Assignee, linked issue key (Jira link), Other Team, linked issue summary. Sorted by priority. Priority and Status columns have color-background cell formatting.

### Issue Counts — Summary Table

Single-row overview. The **Completed** column here uses `removed_at IS NULL AND status_category = 'Done'` without the `resolved_at` window. This reflects live Done status and may include carry-over issues. Use the Completed SP and Completed Issues stat panels for authoritative counts.

### All Issues in Sprint

**Times Carried** — Count of prior sprints the issue appeared in with `removed_at IS NULL AND sprint_id < current_sprint`. Orange ≥ 1, red ≥ 3. Priority and Status columns have color-background cell formatting.

### QASE Test Coverage (collapsed)

Filters to `removed_at IS NULL AND issue_type != 'Epic' AND status NOT IN ('Open', 'Obsolete / Won''t Do')`.

Only issues that are actively in progress or done count toward coverage — open (not yet started) and obsolete issues are excluded from all QASE panels.

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

**Quarter assignment:** Uses `start_date` — sprints are assigned to the quarter they started in. The Velocity and Delivery % panels use `date_trunc('quarter', start_date) = '$quarter'::date`. The Sprint Issue Count panel uses `COALESCE(complete_date, start_date)` for the quarter filter (minor inconsistency within this dashboard). The Sprint Summary Table uses `start_date`.

**Sprint filter:** Project-majority filter — a sprint is included only if more than 50% of its `was_in_initial_scope = TRUE` issues belong to the selected project(s). Consistent with all other dashboards.

All SP metrics flow through `v_planning_deviation`.

| Panel | Source | Notes |
|-------|--------|-------|
| Sprint Velocity — Committed vs Delivered | `v_planning_deviation` | committed_points, delivered_points; `state = 'closed'` only |
| Delivery % per Sprint | `v_planning_deviation` | `delivery_pct`, green ≥ 90%, yellow ≥ 70%, red < 70%; `state = 'closed'` only |
| Sprint Issue Count — Committed vs Delivered | `v_planning_deviation` | committed_issues, delivered_issues; `committed_issues > 0` |
| Sprint Summary Table | `v_planning_deviation` | Includes Deviation = delivered − committed; active sprints shown first |

---

## PO KPIs Dashboard

Quarterly KPIs for Product Owners: planning quality, delivery accuracy, re-work, blockers, release quality.

**Quarter assignment:** Uses `start_date` (sprints assigned to the quarter they started in). Consistent with Sprint Detail and Sprint Overview Quarter.

**Sprint filter:** Project-majority filter (`was_in_initial_scope = TRUE`, >50% committed issues from selected projects). More accurate than simple join for multi-team boards.

### Data Age

**Data Age** — Hours since the most recent completed sync run (any status: success, partial, or error). Queries `sync_log` for the most recent row where `finished_at IS NOT NULL`. Color-coded: green ≤ 12 h · yellow 12–24 h · red > 24 h (syncs run at 07:00 and 19:00 UTC).

### KPI Summary Row 1 — Planning & Quality

**Avg Scope Change %** — `AVG((added_sp + removed_sp) / committed_sp × 100)` across closed sprints in quarter. Target ≤ 10%.

**Avg Planning Accuracy %** — `AVG(delivery_pct)` from `v_planning_deviation` for closed sprints. Since `delivery_pct` only counts committed issues, this cannot exceed 100%.

**Ticket Reopens** — Issues that transitioned from a testing status to In Progress: `LOWER(from_status) LIKE '%test%' AND LOWER(to_status) LIKE '%progress%'`. Filtered by `transitioned_at` quarter.

**Avg Blocker Resolution Time** — `AVG(EXTRACT(EPOCH FROM (resolved_at - created_at))/3600.0)` in hours for Blocker issues where `resolved_at IS NOT NULL` and `date_trunc('quarter', created_at) = '$quarter'::date`. Uses `created_at` for quarter assignment so it measures blockers that originated in the quarter, regardless of when resolved.

### KPI Summary Row 2 — Process Quality

**Avg Bug Closure Rate** — `COUNT(Done bugs) / COUNT(all bugs raised) × 100` for the selected project and quarter. Bug's quarter is determined by `created_at`. Target ≥ 80%.

**Avg Tickets DOD Rate** — Average per sprint of `(Done Stories/Tasks with all DOD criteria) / (all Done Stories/Tasks) × 100`. Denominator = committed tickets with `status = 'Done'` only. DOD criteria: `assignee IS NOT NULL`, `qa_assignee IS NOT NULL`, `SP ≥ 1`, `has_acceptance_criteria = TRUE`, `epic_key IS NOT NULL`. Excludes Epics, Sub-tasks, Bugs. Target ≥ 80%.

**Avg Tickets DOR Rate** — Average per sprint of `(committed Stories/Tasks with all DOR criteria) / (all committed Stories/Tasks) × 100`. DOR criteria: `assignee IS NOT NULL`, `qa_assignee IS NOT NULL`, `SP ≥ 1`, `has_acceptance_criteria = TRUE`, `epic_key IS NOT NULL`, `cardinality(components) > 0`. Excludes Epics, Sub-tasks, Bugs. Target ≥ 80%.

**Avg Release Quality Score** — `SUM(bug_count × weight) / COUNT(distinct releases)` for released versions in the quarter. Weights: Blocker=5, Critical=4, High=3, Medium=2, Low=1. Lower = better. Thresholds: green < 10, yellow 10–25, red ≥ 25.

### Planning & Delivery Charts

**Scope Change % per Sprint** — Per-sprint bar chart of `(added_sp + removed_sp) / committed_sp`. Ordered by `start_date ASC`.

**Planning Accuracy % per Sprint** — Shows Committed SP, Delivered SP, and Delivery % per sprint from `v_planning_deviation`. Ordered oldest to newest.

**Blocker Resolution Time per Sprint** — `AVG((resolved_at - created_at) / 3600)` per sprint for Blocker issues. Uses `was_in_initial_scope = TRUE` to avoid carry-over double-counting.

**Open Blockers** — Currently open (`status_category != 'Done'`) Blocker issues, ordered by age. No time filter — shows all open blockers regardless of quarter. Table columns: Key, Summary, Assignee, Status, Age (days).

**Weekly Ticket Reopen Rate** — Time-series using Grafana `$__timeFilter`. Spike = quality regression.

**Most Reopened Issues** — Issues with most reopen transitions in selected quarter.

### Release Bug Quality

Links `issues.fix_versions` (array) to `releases` via `r.name = ANY(i.fix_versions) AND r.project_key = i.project_key`. Quarter filtered by `release_date`.

**Major Bugs in Releases** — Count of Blocker+Critical+High bugs in released versions. Thresholds: green < 10, yellow 10–30, orange 30–60, red ≥ 60.

**Blocker Bugs in Releases** — Blocker-only count. Thresholds: green < 3, yellow 3–10, orange 10–20, red ≥ 20.

**Total Bugs in Releases** — All priorities combined.

**Bugs per Release (bar chart)** — Stacked by priority (dark-red=Blocker, red=Critical, orange=High, yellow=Medium, green=Low), ordered by `release_date ASC`.

**Major Bug Details** — Table of Blocker/Critical/High bugs with release name, date, priority, status, assignee, Jira link. Priority and Status columns have color-background cell formatting.

---

## Team Overview Dashboard

Per-assignee metrics for selected team and quarter/sprint.

**Sprint vs Quarter mode:** All panels support both. When `$sprint = 0` (All), filters by `date_trunc('quarter', resolved_at) = '$quarter'::date`. When a sprint is selected, filters by `key IN (SELECT issue_key FROM sprint_issues WHERE sprint_id = $sprint AND removed_at IS NULL)`.

**Note — story points field:** This dashboard uses `issues.story_points` (current value) rather than `sprint_issues.story_points_at_add`. This is intentional for WIP display (current workload) but means delivery SP may differ slightly from Sprint Detail.

**Obsolete exclusion:** Issues marked "Obsolete / Won't Do" are excluded from all completed issue counts (`status != 'Obsolete / Won''t Do'`), consistent with Sprint Detail.

**Sprint time window:** When a sprint is selected, completed issue panels enforce `resolved_at >= sprint.start_date AND resolved_at <= COALESCE(sprint.complete_date, sprint.end_date, NOW())`. This prevents counting issues that were technically resolved after the sprint closed.

| Panel | What it measures |
|-------|-----------------|
| Current WIP per Assignee | Open `In Progress` issues, current `story_points` |
| Avg Cycle Time (RFT→Done) per Assignee | From `v_cycle_time_rft_to_done`, avg `hours_rft_to_done` |
| Issues Completed per Assignee | `status_category = 'Done'` AND `status != 'Obsolete / Won''t Do'` count + SP, filtered by quarter or sprint window |
| Where Issues Spend Most Time | From `v_time_in_status`, avg hours per status, ≥ 5 issues, < 8760h (1yr cap) |
| Throughput Trend (weekly) | `DATE_TRUNC('week', resolved_at)` count per assignee; excludes Obsolete |
| Assignee Detail Table | Summary: issues completed, SP, bugs fixed, avg cycle time, current WIP; excludes Obsolete |

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

**Release filter:** `$release = 'ALL' OR $release = ANY(fix_versions)` — when a release is selected, only bugs linked to that fix version are shown (for standard bug panels).

**Quarter assignment for QASE panels:** Uses `COALESCE(s.complete_date, s.start_date)` for sprint-quarter linkage. This differs from Sprint Detail which uses `start_date`. QASE coverage tied to when a sprint shipped is the intended behaviour.

**QASE panels exclude Epics, Sub-tasks, Open status, and Obsolete / Won't Do status** — only issues actively in progress or done count toward coverage.

### Bug Metrics

| Panel | What it counts | Priority filter |
|-------|----------------|----------------|
| Open Blocker Bugs | `issue_type = 'Bug' AND status_category != 'Done' AND priority = 'Blocker'` | Blocker |
| Open Critical Bugs | `issue_type = 'Bug' AND status_category != 'Done' AND priority = 'Critical'` | Critical |
| Open High Bugs | `issue_type = 'Bug' AND status_category != 'Done' AND priority = 'High'` | High |
| Open Medium & Low Bugs | `issue_type = 'Bug' AND status_category != 'Done' AND priority IN ('Medium','Low')` | Medium + Low |

These panels do not exclude Obsolete — `status_category != 'Done'` is the only filter besides priority. Release filter applies (`$release = 'ALL' OR $release = ANY(fix_versions)`).

**Bug Creation Rate** — Weekly `COUNT(*)` by priority using `$__timeFilter(created_at)`.

**Bugs per Release** — `UNNEST(fix_versions)` join, filtered by `created_at` quarter. Shows Total, Resolved, Open per release.

**Bug Resolution Rate vs Creation Rate** — Two time-series: `COUNT(*)` by `created_at` vs `resolved_at` per week.

**Open Bugs — Critical & High** — `status_category != 'Done' AND priority IN ('Blocker','Critical')`, ordered by age ASC (oldest first). Priority and Status columns have color-background cell formatting.

### Release Bug History (Initial Quality)

This section requires a specific release to be selected (`$release != 'ALL'`) — all panels in this section return no data when "ALL" is selected.

Uses `issue_fix_version_history` to include bugs that were ever assigned to the selected release, even if subsequently moved to a different fix version. All panels exclude `status = 'Obsolete / Won''t Do'`.

| Panel | Logic |
|-------|-------|
| Bugs Ever in Release | `COUNT(DISTINCT i.key)` where `$release = ANY(fix_versions) OR EXISTS (SELECT 1 FROM issue_fix_version_history fvh WHERE fvh.issue_key = i.key AND fvh.fix_version = $release)` |
| Bugs Moved to Another Release | Same as above but additionally requires `NOT ($release = ANY(i.fix_versions))` — i.e. was in release historically but is no longer. Thresholds: green < 3, yellow 3–9, red ≥ 10. |
| Major Bugs Ever in Release | Same as "Bugs Ever in Release" filtered to `priority IN ('Blocker', 'Critical', 'High')`. Thresholds: green < 3, yellow 3–9, orange 10–19, red ≥ 20. |
| All Bugs Ever Assigned to Release | Detail table: Key, Summary, Priority, Status, Assignee, Still in Release (✅ Yes / ➡ Moved), Current Fix Version(s). Ordered by: still-in-release first, then priority, then key. Priority column has color-background. |

### QASE Coverage (Quality & Bugs)

All panels filter to `issue_type NOT IN ('Epic','Sub-task') AND status NOT IN ('Open', 'Obsolete / Won''t Do')` and use sprint-quarter linkage via:

```sql
key IN (
  SELECT DISTINCT si.issue_key
  FROM sprint_issues si
  JOIN sprints s ON s.id = si.sprint_id
  WHERE date_trunc('quarter', COALESCE(s.complete_date, s.start_date)) = '$quarter'::date
)
```

The release filter also applies: `$release = 'ALL' OR $release = ANY(fix_versions)`.

| Panel | Logic |
|-------|-------|
| Issues with QASE Link | `has_qase_link = TRUE` |
| Issues Confirmed Without QASE Link | `has_qase_link = FALSE` |
| QASE Coverage % | `COUNT(TRUE) / COUNT(NOT NULL)` — excludes unchecked from denominator |
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

---

## Color Coding — All Table Panels

All table panels across all dashboards apply `color-background` cell formatting to **Priority** and **Status** columns where present.

**Priority color mapping:**

| Priority | Color |
|----------|-------|
| Blocker | dark-red |
| Critical | red |
| High | orange |
| Medium | yellow |
| Low | green |

Status columns use threshold-based color backgrounds. Specific thresholds vary by context (e.g. the "Still in Release" column in the Release Bug History table uses green/red based on whether the bug is still assigned to the selected release).

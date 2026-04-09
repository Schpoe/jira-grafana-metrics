# Sprint Detail Dashboard — Metrics Reference

**Dashboard purpose:** Per-sprint drill-down for Scrum Masters and Release Managers.
Select a team and sprint to see scope, readiness, delivery, and QASE test coverage.

**Data source:** PostgreSQL (`jira-metrics-pg`). All metrics derive from
`sprint_issues`, `issues`, `sprints`, `issue_transitions`, and the
`v_planning_deviation` view.

**Common exclusions (unless noted):** All story-point and issue-count panels exclude:
- `issue_type IN ('Epic', 'Sub-task')`
- `status = 'Obsolete / Won''t Do'`
- `removed_at IS NOT NULL` (only currently active sprint members)

---

## Story Points

### Committed
Total story points planned at sprint start.

**SQL logic:** `SUM(COALESCE(story_points_at_add, story_points, 0))` where
`was_in_initial_scope = TRUE AND removed_at IS NULL`, excluding Epics, Sub-tasks,
and Obsolete.

`story_points_at_add` is the estimate recorded when the issue entered the sprint.
It is preferred over the current `issues.story_points` so that mid-sprint re-estimation
does not retroactively change what was committed.

---

### Unplanned
Story points added after sprint start.

**SQL logic:** Same SUM as Committed but filtered to
`was_in_initial_scope = FALSE AND removed_at IS NULL`.

**Target:** ≤ 10% of Committed.

---

### Total in Sprint
All active story points (committed + unplanned, excluding removed).

**SQL logic:** `SUM(COALESCE(story_points_at_add, story_points, 0))` where
`removed_at IS NULL`, excluding Epics, Sub-tasks, Obsolete.

---

### Obsolete / Won't Do
Story points on issues currently in the sprint that are marked Obsolete.

**SQL logic:** `SUM(COALESCE(story_points_at_add, story_points, 0))` where
`removed_at IS NULL AND status = 'Obsolete / Won''t Do'`.

No issue-type filter is applied so nothing is silently hidden. These issues are
excluded from all other SP counters and from velocity calculations.

---

### Completed
Story points delivered within this sprint's time window.

**SQL logic:** `SUM(COALESCE(story_points_at_add, story_points, 0))` where:
- `status_category = 'Done'`
- `status != 'Obsolete / Won''t Do'`
- `issue_type NOT IN ('Epic', 'Sub-task')`
- `resolved_at IS NOT NULL`
- `resolved_at >= sprint.start_date`
- `resolved_at <= COALESCE(sprint.complete_date, sprint.end_date, NOW())`

**Why the `resolved_at` window matters:** Issues can appear in many consecutive
sprints with `removed_at IS NULL` due to Jira carry-over behaviour (an issue
carried from sprint A to sprint B exists in `sprint_issues` for both with no
`removed_at`). Without the window filter, a Done issue would be counted in every
sprint it ever touched. The `resolved_at` window credits delivery to exactly one
sprint — the one whose `[start_date, complete_date]` interval contains the resolution
timestamp.

---

### Delivery %
`ROUND(100.0 * delivered_points / committed_points, 1)` from `v_planning_deviation`.

Displayed as a gauge: red < 60%, yellow 60–80%, green ≥ 80%.

See the [v_planning_deviation view](#v_planning_deviation-view) section for full details.

---

### Avg Velocity (Last 6 Sprints)
Average delivered story points over the 6 most recent closed sprints for the same team.

**SQL logic:**
1. Find closed sprints (`state = 'closed'`) whose `complete_date` is before the
   current sprint's `start_date` — this gives a true running average: each sprint
   shows the velocity of the 6 sprints that preceded it.
2. Apply the **project-majority filter**: include a historical sprint only if more
   than 50% of its committed issues (`was_in_initial_scope = TRUE`) share at least
   one `project_key` with the current sprint's committed issues. This prevents
   cross-team contamination when multiple teams share a board.
3. `AVG(delivered_points)` from `v_planning_deviation` across those 6 sprints.

**No board_id filter is applied.** The STORE team, for example, alternates between
board IDs 100 and 153 across sprints; filtering by board would skip half their history.
The project-majority filter alone is sufficient.

---

### Velocity Booked %
What fraction of average velocity has been committed in the current sprint.

**SQL logic:**
```
ROUND(100.0 * committed_sp / avg_velocity, 1)
```
- `committed_sp`: same as the Committed stat panel (Epics/Sub-tasks/Obsolete excluded)
- `avg_velocity`: same 6-sprint / project-majority formula as above

**Thresholds:** green < 80%, yellow 80–100%, red > 100% (display capped at 150%).

---

## Story Readiness

### Planned
Count of issues in the initial sprint scope.

**SQL logic:** `COUNT(*) WHERE was_in_initial_scope = TRUE AND
issue_type NOT IN ('Epic', 'Sub-task')`.

Note: no `removed_at` filter — issues that were in initial scope and later removed
are still counted. Bugs are included.

---

### Ready Issues
Count of issues that satisfy all three readiness conditions simultaneously.

**SQL logic:** `COUNT(*) WHERE removed_at IS NULL AND
issue_type NOT IN ('Epic', 'Sub-task') AND story_points > 0 AND
epic_key IS NOT NULL AND has_acceptance_criteria = TRUE`.

An issue is only "ready" when it has: a size estimate, an Epic link, and documented
acceptance criteria.

---

### Missing Assignee
Issues (all types) currently in the sprint with no assignee.

**SQL logic:** `COUNT(*) WHERE removed_at IS NULL AND assignee IS NULL AND
status != 'Obsolete / Won''t Do'`.

No issue-type filter — Epics and Sub-tasks are included because they also need owners.

---

### Missing AC
Stories and Tasks without documented acceptance criteria.

**SQL logic:** `COUNT(*) WHERE removed_at IS NULL AND
status != 'Obsolete / Won''t Do' AND
issue_type NOT IN ('Epic', 'Sub-task', 'Bug') AND
(has_acceptance_criteria IS NULL OR has_acceptance_criteria = FALSE)`.

Bugs are excluded because bug reports do not require acceptance criteria.

---

### Missing SP
Issues without a story point estimate.

**SQL logic:** `COUNT(*) WHERE removed_at IS NULL AND
issue_type NOT IN ('Epic', 'Sub-task') AND
(story_points IS NULL OR story_points = 0)`.

---

### Missing Epic Link
Issues not linked to a parent Epic.

**SQL logic:** `COUNT(*) WHERE removed_at IS NULL AND
issue_type NOT IN ('Epic', 'Sub-task') AND epic_key IS NULL`.

---

### Readiness %
Fraction of active sprint issues that are fully ready (SP + Epic + AC).

**SQL logic:**
```sql
ROUND(100.0 *
  COUNT(*) FILTER (WHERE story_points > 0 AND epic_key IS NOT NULL AND has_acceptance_criteria = TRUE)
  / NULLIF(COUNT(*), 0), 1)
```
over `removed_at IS NULL AND issue_type NOT IN ('Epic', 'Sub-task')`.

The denominator includes Bugs even though Bugs are not required to have AC. A Bug
with SP and Epic link is counted in the numerator even without AC. This means the
percentage slightly understates readiness for sprints with many Bugs.

**Target:** ≥ 90% (green). Thresholds: red < 70%, yellow 70–90%.

---

### Completed Issues
Count of issues resolved within this sprint's time window.

**SQL logic:** Same `resolved_at` sprint-window filter as the Completed SP panel.
Excludes Epics, Sub-tasks, Obsolete.

---

### Open Issues
Issues still in progress at query time.

**SQL logic:** `COUNT(*) WHERE removed_at IS NULL AND
issue_type NOT IN ('Epic', 'Sub-task') AND status != 'Obsolete / Won''t Do' AND
status_category != 'Done'`.

Uses current `status_category`, not `resolved_at`, so this reflects the live state.

---

### Issues Not Ready (detail table)
Every active non-Epic/Sub-task/Obsolete issue failing at least one readiness check.

| Column   | Fail condition |
|----------|---------------|
| SP       | `story_points IS NULL OR story_points = 0` |
| Epic     | `epic_key IS NULL` |
| AC       | `issue_type != 'Bug' AND has_acceptance_criteria IS NOT TRUE` (Bugs show N/A) |
| Assignee | `assignee IS NULL` (shown as name or `—`) |

An issue appears in the table if **any** of these conditions is true. Bugs only
appear for missing SP, Epic, or Assignee — not for missing AC.

Sorted by priority (Blocker → Critical → High → Medium → other).

---

## Scope Change

### Issues Added / SP Added
Issues and story points added after sprint start (`was_in_initial_scope = FALSE AND
removed_at IS NULL`). No issue-type filter.

### Issues Removed / SP Removed
Issues and story points removed from the sprint (`removed_at IS NOT NULL`). No
issue-type filter.

### Scope Change %
Total churn relative to committed scope.

```
ROUND(100.0 * (SP_added + SP_removed) / NULLIF(committed_SP, 0), 1)
```

Note: the committed denominator here (`was_in_initial_scope = TRUE`, no type
filters) differs from the Committed stat panel which excludes Epics/Sub-tasks/Obsolete.
This is intentional — the Scope Change % uses raw sprint report data.

**Target:** ≤ 10%. Thresholds: orange at 10%, red at 25%.

---

## Issue Counts — Summary Table

Single-row overview. The Completed column uses `removed_at IS NULL AND
status_category = 'Done'` without the `resolved_at` sprint-window filter.
This reflects the current Done state of all active issues and may include
carry-over issues that happen to be Done now. For the authoritative
sprint-window-bounded counts use the Completed SP (id=7) and Completed Issues
(id=102) panels.

---

## All Issues in Sprint

Full issue list with: Key, Summary, Type, Priority, Assignee, Status, SP, Epic,
Scope (Committed/Unplanned), Rmvd, Times Carried.

**Times Carried:** How many prior sprints the issue appeared in with
`removed_at IS NULL AND sprint_id < current_sprint`. Orange ≥ 1, red ≥ 3.

Sort order: active issues first (removed last), then In Progress → other → Done,
then by priority.

---

## QASE Test Coverage (collapsed)

All panels filter to `removed_at IS NULL AND issue_type != 'Epic'`.
Sub-tasks are included (unlike SP panels).

| Panel | Logic |
|-------|-------|
| With QASE Link | `has_qase_link = TRUE` |
| Without QASE Link | `has_qase_link = FALSE` |
| Not Yet Checked | `has_qase_link IS NULL` — sync has not checked yet |
| QASE Coverage % | `COUNT(TRUE) / COUNT(NOT NULL)` — excludes unchecked from denominator |

Coverage % thresholds: red < 50%, orange 50–80%, green ≥ 80%.

---

## Burndown Chart

Daily remaining SP vs. an ideal linear burndown line.

**Steps:**
1. **Committed issues:** `was_in_initial_scope = TRUE` for the selected sprint.
2. **Total SP:** sum of committed SP (burndown start value).
3. **Done per issue:** earliest `issue_transitions.transitioned_at` where
   `UPPER(to_status) = 'DONE'` and `transitioned_at >= sprint.start_date`.
4. **Remaining SP (actual):** for each day, `total_sp − SUM(sp for issues Done
   before end of that day)`. NULL for future days.
5. **Ideal line:** `total_sp × (1 − elapsed_days / sprint_duration)`, floored at 0.

Chart extends two days past sprint end to show final state.

---

## `v_planning_deviation` View

Defined in `init.sql`. Used by Delivery % and both velocity panels.

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
WHERE status_category = 'Done'
  AND status != 'Obsolete / Won''t Do'
  AND issue_type NOT IN ('Epic', 'Sub-task')
  AND resolved_at IS NOT NULL
  AND resolved_at >= sprint.start_date
  AND resolved_at <= COALESCE(sprint.complete_date, sprint.end_date, NOW())
```
- `delivered_points`: `SUM(COALESCE(story_points_at_add, story_points, 0))`
- `delivered_issues`: `COUNT(*)`

**Why `resolved_at` instead of `removed_at IS NULL`:** A Done issue can have
`removed_at IS NULL` in a dozen consecutive sprints due to Jira carry-over behaviour,
causing massive double-counting if we simply join on `sprint_id`. The `resolved_at`
window credits each delivery to exactly one sprint.

**Important:** `resolved_at` is backfilled from `issue_transitions` using
`MAX(transitioned_at) WHERE LOWER(to_status) = LOWER(current_status)`. All Done
issues should have `resolved_at` populated after the backfill step.

### Final columns

| Column | Formula |
|--------|---------|
| `deviation_points` | `delivered_points − committed_points` |
| `delivery_pct` | `ROUND(100.0 * delivered_points / committed_points, 1)` — NULL when committed = 0 |

The view LEFT JOINs all sprints so sprints with zero activity still appear.

---

## Variable Filters

### Quarter
Derived from `issues.created_at` — shows distinct quarters in descending order.

### Team / Project
Multi-select list of all `projects.key` values. Controls which sprints appear in
the Sprint variable.

### Sprint
Filtered to sprints whose `start_date` falls in the selected quarter
(`date_trunc('quarter', s.start_date) = '$quarter'::date`) and where more than 50%
of committed issues belong to the selected project(s) — the project-majority filter.
Active sprints are shown first (marked ★), then sorted by `start_date` descending.

The project-majority filter uses `was_in_initial_scope = TRUE` (not `removed_at IS NULL`)
to avoid contamination from carry-over issues that accumulate across many sprints.

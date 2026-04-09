-- Jira Metrics Schema

CREATE TABLE IF NOT EXISTS projects (
    key         TEXT PRIMARY KEY,
    name        TEXT,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sprints (
    id             INTEGER PRIMARY KEY,
    board_id       INTEGER,
    name           TEXT,
    state          TEXT,  -- future, active, closed
    start_date     TIMESTAMPTZ,
    end_date       TIMESTAMPTZ,
    complete_date  TIMESTAMPTZ,
    goal             TEXT,
    report_synced_at TIMESTAMPTZ,   -- set after sprint report is fetched from Jira
    synced_at        TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE sprints ADD COLUMN IF NOT EXISTS report_synced_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS issues (
    key             TEXT PRIMARY KEY,
    project_key     TEXT REFERENCES projects(key),
    summary         TEXT,
    issue_type      TEXT,
    status          TEXT,
    status_category TEXT,  -- "To Do", "In Progress", "Done"
    priority        TEXT,
    story_points    NUMERIC,
    assignee        TEXT,
    reporter        TEXT,
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    fix_versions    TEXT[],
    labels          TEXT[],
    epic_key        TEXT,        -- parent Epic key (null for Epics themselves and unlinked issues)
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);
-- Migration: add column to existing databases
ALTER TABLE issues ADD COLUMN IF NOT EXISTS epic_key TEXT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS has_qase_link BOOLEAN;  -- NULL = not yet checked
ALTER TABLE issues ADD COLUMN IF NOT EXISTS has_acceptance_criteria BOOLEAN;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS customer_project TEXT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS customer TEXT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS project_name TEXT;

-- Sprint membership with scope-change tracking
CREATE TABLE IF NOT EXISTS sprint_issues (
    sprint_id              INTEGER REFERENCES sprints(id),
    issue_key              TEXT REFERENCES issues(key),
    added_at               TIMESTAMPTZ DEFAULT NOW(),
    removed_at             TIMESTAMPTZ,          -- NULL = still in sprint
    was_in_initial_scope   BOOLEAN DEFAULT FALSE, -- set when sprint starts
    story_points_at_add    NUMERIC,
    PRIMARY KEY (sprint_id, issue_key)
);

-- Full status-transition history (powers cycle time queries)
CREATE TABLE IF NOT EXISTS issue_transitions (
    id               SERIAL PRIMARY KEY,
    issue_key        TEXT REFERENCES issues(key),
    from_status      TEXT,
    to_status        TEXT,
    transitioned_at  TIMESTAMPTZ,
    author           TEXT,
    UNIQUE (issue_key, transitioned_at, to_status)
);

-- Fix versions / releases
CREATE TABLE IF NOT EXISTS releases (
    id           TEXT PRIMARY KEY,  -- Jira version ID
    project_key  TEXT REFERENCES projects(key),
    name         TEXT,
    description  TEXT,
    release_date DATE,
    released     BOOLEAN DEFAULT FALSE,
    archived     BOOLEAN DEFAULT FALSE,
    synced_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Sync audit log
CREATE TABLE IF NOT EXISTS sync_log (
    id                  SERIAL PRIMARY KEY,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    issues_synced       INTEGER DEFAULT 0,
    sprints_synced      INTEGER DEFAULT 0,
    transitions_synced  INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'running',  -- running, success, partial, error
    error_message       TEXT,
    next_page_token     TEXT,   -- checkpoint: resume issues sync from this token
    issues_checkpoint   INTEGER DEFAULT 0,  -- issues synced so far in this run
    transitions_checkpoint INTEGER DEFAULT 0
);

-- ─── Indexes ────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_issues_project      ON issues(project_key);
CREATE INDEX IF NOT EXISTS idx_issues_status       ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_type         ON issues(issue_type);
CREATE INDEX IF NOT EXISTS idx_issues_resolved     ON issues(resolved_at);
CREATE INDEX IF NOT EXISTS idx_issues_updated      ON issues(updated_at);

CREATE INDEX IF NOT EXISTS idx_transitions_key     ON issue_transitions(issue_key);
CREATE INDEX IF NOT EXISTS idx_transitions_to      ON issue_transitions(to_status);
CREATE INDEX IF NOT EXISTS idx_transitions_at      ON issue_transitions(transitioned_at);

CREATE INDEX IF NOT EXISTS idx_sprint_issues_sprint ON sprint_issues(sprint_id);
CREATE INDEX IF NOT EXISTS idx_sprint_issues_issue  ON sprint_issues(issue_key);

CREATE INDEX IF NOT EXISTS idx_releases_project    ON releases(project_key);
CREATE INDEX IF NOT EXISTS idx_releases_date       ON releases(release_date);

-- Application settings (key/value pairs written by the sync process)
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Historical fix version assignments per issue
-- Tracks every fixVersion ever assigned to an issue via changelog,
-- even if the issue was later moved to a different release.
CREATE TABLE IF NOT EXISTS issue_fix_version_history (
    issue_key       TEXT NOT NULL,
    fix_version     TEXT NOT NULL,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01',
    removed_at      TIMESTAMPTZ,   -- NULL if still assigned
    PRIMARY KEY (issue_key, fix_version, added_at)
);
CREATE INDEX IF NOT EXISTS idx_ifvh_issue    ON issue_fix_version_history(issue_key);
CREATE INDEX IF NOT EXISTS idx_ifvh_version  ON issue_fix_version_history(fix_version);

-- Issue links (e.g., Epic "implements" PROD item)
CREATE TABLE IF NOT EXISTS issue_links (
    from_key    TEXT NOT NULL,   -- source issue (e.g. Epic key)
    to_key      TEXT NOT NULL,   -- target issue (e.g. PROD-xxx)
    to_summary  TEXT,            -- summary of the linked issue
    link_type   TEXT NOT NULL,   -- e.g. "Polaris work item link", "Implement"
    link_label  TEXT,            -- e.g. "implements"
    direction   TEXT NOT NULL,   -- "outward" or "inward"
    synced_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (from_key, to_key, link_type, direction)
);
ALTER TABLE issue_links ADD COLUMN IF NOT EXISTS to_summary TEXT;

CREATE INDEX IF NOT EXISTS idx_issue_links_from ON issue_links(from_key);
CREATE INDEX IF NOT EXISTS idx_issue_links_to   ON issue_links(to_key);

-- History of link additions and removals
CREATE TABLE IF NOT EXISTS issue_link_history (
    id          SERIAL PRIMARY KEY,
    from_key    TEXT NOT NULL,
    to_key      TEXT NOT NULL,
    to_summary  TEXT,
    link_type   TEXT NOT NULL,
    link_label  TEXT,
    direction   TEXT NOT NULL,
    event       TEXT NOT NULL,       -- 'added' or 'removed'
    occurred_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_link_history_from ON issue_link_history(from_key);
CREATE INDEX IF NOT EXISTS idx_link_history_to   ON issue_link_history(to_key);
CREATE INDEX IF NOT EXISTS idx_link_history_at   ON issue_link_history(occurred_at);

-- ─── Useful Views ────────────────────────────────────────────────────────────

-- Cycle time: time in each status per issue
CREATE OR REPLACE VIEW v_time_in_status AS
SELECT
    t.issue_key,
    t.from_status                                                   AS status,
    t.transitioned_at                                               AS entered_at,
    LEAD(t.transitioned_at) OVER (
        PARTITION BY t.issue_key ORDER BY t.transitioned_at
    )                                                               AS exited_at,
    EXTRACT(EPOCH FROM (
        LEAD(t.transitioned_at) OVER (
            PARTITION BY t.issue_key ORDER BY t.transitioned_at
        ) - t.transitioned_at
    )) / 3600.0                                                     AS hours_in_status
FROM issue_transitions t;

-- Cycle time: In Progress → Ready For Testing (in hours)
CREATE OR REPLACE VIEW v_cycle_time_in_progress_to_rft AS
SELECT
    rft.issue_key,
    i.issue_type,
    i.priority,
    i.story_points,
    i.project_key,
    ip.transitioned_at                                              AS entered_in_progress_at,
    rft.transitioned_at                                             AS entered_rft_at,
    ROUND(
        EXTRACT(EPOCH FROM (rft.transitioned_at - ip.transitioned_at)) / 3600.0,
        2
    )                                                               AS hours_in_progress_to_rft
FROM issue_transitions rft
JOIN issue_transitions ip
    ON  ip.issue_key          = rft.issue_key
    AND LOWER(ip.to_status)   = 'in progress'
    AND ip.transitioned_at    < rft.transitioned_at
JOIN issues i ON i.key = rft.issue_key
WHERE LOWER(rft.to_status) = 'ready for testing'
  -- most recent In Progress before this RFT entry
  AND NOT EXISTS (
      SELECT 1 FROM issue_transitions x
      WHERE x.issue_key          = ip.issue_key
        AND LOWER(x.to_status)   = 'in progress'
        AND x.transitioned_at    > ip.transitioned_at
        AND x.transitioned_at    < rft.transitioned_at
  );

-- Cycle time: Ready For Testing → Done (in hours)
CREATE OR REPLACE VIEW v_cycle_time_rft_to_done AS
SELECT
    rft.issue_key,
    i.issue_type,
    i.priority,
    i.story_points,
    i.project_key,
    rft.transitioned_at                                             AS entered_rft_at,
    done.transitioned_at                                            AS resolved_at,
    ROUND(
        EXTRACT(EPOCH FROM (done.transitioned_at - rft.transitioned_at)) / 3600.0,
        2
    )                                                               AS hours_rft_to_done
FROM issue_transitions rft
JOIN issue_transitions done
    ON  done.issue_key     = rft.issue_key
    AND UPPER(done.to_status) = 'DONE'
    AND done.transitioned_at > rft.transitioned_at
JOIN issues i ON i.key = rft.issue_key
WHERE LOWER(rft.to_status) = 'ready for testing'
  -- take only the first Done transition after RFT
  AND NOT EXISTS (
      SELECT 1 FROM issue_transitions x
      WHERE x.issue_key = done.issue_key
        AND UPPER(x.to_status) = 'DONE'
        AND x.transitioned_at > rft.transitioned_at
        AND x.transitioned_at < done.transitioned_at
  );

-- Sprint scope changes: issues added/removed after sprint start
CREATE OR REPLACE VIEW v_sprint_scope_changes AS
SELECT
    si.sprint_id,
    s.name                                                          AS sprint_name,
    s.state                                                         AS sprint_state,
    si.issue_key,
    i.issue_type,
    i.story_points,
    CASE
        WHEN si.was_in_initial_scope = FALSE AND si.removed_at IS NULL THEN 'added'
        WHEN si.removed_at IS NOT NULL                                  THEN 'removed'
        ELSE 'original'
    END                                                             AS scope_change_type,
    si.added_at,
    si.removed_at
FROM sprint_issues si
JOIN sprints s  ON s.id  = si.sprint_id
JOIN issues  i  ON i.key = si.issue_key;

-- Planning deviation per sprint (committed vs delivered story points/issues)
-- Both committed and delivered are computed directly from sprint_issues + issues,
-- removing dependency on sprint_snapshots which may be missing for many sprints.
-- Committed = was_in_initial_scope=TRUE (from Jira sprint report API).
-- Delivered = issues resolved (resolved_at) within the sprint window.
--   Using resolved_at prevents double-counting carry-over issues that accumulate
--   in sprint_issues across many sprints with removed_at IS NULL.
CREATE OR REPLACE VIEW v_planning_deviation AS
WITH committed AS (
    SELECT
        si.sprint_id,
        COUNT(*)                                                                AS committed_issues,
        COALESCE(SUM(COALESCE(si.story_points_at_add, i.story_points, 0)), 0)  AS committed_points
    FROM sprint_issues si
    JOIN issues i ON i.key = si.issue_key
    WHERE si.was_in_initial_scope = TRUE
      AND si.removed_at IS NULL
      AND i.issue_type NOT IN ('Epic', 'Sub-task')
      AND i.status != 'Obsolete / Won''t Do'
    GROUP BY si.sprint_id
),
delivered AS (
    -- Count committed issues (was_in_initial_scope=TRUE) that are Done.
    -- No resolved_at window needed: was_in_initial_scope=TRUE is unique per sprint
    -- (Jira only sets this for issues explicitly planned at sprint start), so there
    -- is no carry-over double-counting risk. The resolved_at window was excluding
    -- issues resolved slightly outside the sprint window (e.g. after close date),
    -- causing severely understated delivery percentages.
    SELECT
        si.sprint_id                                                            AS sprint_id,
        COUNT(*)                                                                AS delivered_issues,
        COALESCE(SUM(COALESCE(si.story_points_at_add, i.story_points, 0)), 0)  AS delivered_points
    FROM sprint_issues si
    JOIN issues i ON i.key = si.issue_key
    WHERE si.was_in_initial_scope = TRUE
      AND si.removed_at IS NULL
      AND i.status_category = 'Done'
      AND i.status != 'Obsolete / Won''t Do'
      AND i.issue_type NOT IN ('Epic', 'Sub-task')
    GROUP BY si.sprint_id
)
SELECT
    s.id                                                            AS sprint_id,
    s.name                                                          AS sprint_name,
    s.state,
    s.start_date,
    s.complete_date,
    COALESCE(c.committed_points, 0)                                AS committed_points,
    COALESCE(c.committed_issues, 0)                                AS committed_issues,
    COALESCE(d.delivered_points, 0)                                AS delivered_points,
    COALESCE(d.delivered_issues, 0)                                AS delivered_issues,
    COALESCE(d.delivered_points, 0) - COALESCE(c.committed_points, 0) AS deviation_points,
    CASE
        WHEN COALESCE(c.committed_points, 0) > 0
        THEN ROUND(
            100.0 * COALESCE(d.delivered_points, 0) / c.committed_points,
            1
        )
        ELSE NULL
    END                                                             AS delivery_pct
FROM sprints s
LEFT JOIN committed c ON c.sprint_id = s.id
LEFT JOIN delivered d ON d.sprint_id = s.id;

-- Lead time: created → resolved (excludes Epics and Sub-tasks)
CREATE OR REPLACE VIEW v_lead_time AS
SELECT
    key,
    issue_type,
    priority,
    story_points,
    project_key,
    assignee,
    summary,
    created_at,
    resolved_at,
    ROUND(EXTRACT(EPOCH FROM (resolved_at - created_at)) / 3600.0,  2) AS hours_lead_time,
    ROUND(EXTRACT(EPOCH FROM (resolved_at - created_at)) / 86400.0, 2) AS days_lead_time
FROM issues
WHERE resolved_at IS NOT NULL
  AND created_at  IS NOT NULL
  AND issue_type NOT IN ('Epic', 'Sub-task');

-- PROD item progress: aggregated completion across all linked Epics + their child issues
-- An Epic may have multiple rows in issue_links for the same PROD item (different link_type
-- values, e.g. "Polaris work item link" and "Implement", both with link_label = 'implements').
-- The CTE deduplicates to one row per (Epic, PROD) pair so JOINs to child issues don't
-- multiply counts and sums.
CREATE OR REPLACE VIEW v_prod_item_progress AS
WITH deduped_links AS (
    SELECT DISTINCT ON (from_key, to_key) from_key, to_key, to_summary
    FROM issue_links
    WHERE to_key LIKE 'PROD-%' AND link_label = 'implements'
    ORDER BY from_key, to_key
)
SELECT
    dl.to_key                                                               AS prod_key,
    MAX(dl.to_summary)                                                      AS prod_summary,
    COUNT(DISTINCT dl.from_key)                                             AS epic_count,
    COUNT(DISTINCT ci.key)                                                  AS total_issues,
    COUNT(DISTINCT ci.key) FILTER (WHERE ci.status_category = 'Done')      AS done_issues,
    COALESCE(SUM(ci.story_points), 0)                                       AS total_sp,
    COALESCE(SUM(ci.story_points) FILTER (WHERE ci.status_category = 'Done'), 0) AS done_sp,
    ROUND(
        100.0 * COUNT(DISTINCT ci.key) FILTER (WHERE ci.status_category = 'Done')
        / NULLIF(COUNT(DISTINCT ci.key), 0), 1
    )                                                                       AS completion_pct_issues,
    ROUND(
        100.0 * COALESCE(SUM(ci.story_points) FILTER (WHERE ci.status_category = 'Done'), 0)
        / NULLIF(SUM(ci.story_points), 0), 1
    )                                                                       AS completion_pct_sp
FROM deduped_links dl
JOIN issues i ON i.key = dl.from_key
LEFT JOIN issues ci
    ON  ci.epic_key    = dl.from_key
    AND ci.issue_type NOT IN ('Epic', 'Sub-task')
GROUP BY dl.to_key;

-- Per-Epic progress for a given PROD item (drill-down)
CREATE OR REPLACE VIEW v_prod_epic_progress AS
WITH deduped_links AS (
    SELECT DISTINCT ON (from_key, to_key) from_key, to_key, to_summary
    FROM issue_links
    WHERE to_key LIKE 'PROD-%' AND link_label = 'implements'
    ORDER BY from_key, to_key
)
SELECT
    dl.to_key                                                               AS prod_key,
    MAX(dl.to_summary)                                                      AS prod_summary,
    dl.from_key                                                             AS epic_key,
    MAX(i.summary)                                                          AS epic_summary,
    MAX(i.project_key)                                                      AS project_key,
    MAX(i.status)                                                           AS epic_status,
    COUNT(DISTINCT ci.key)                                                  AS total_issues,
    COUNT(DISTINCT ci.key) FILTER (WHERE ci.status_category = 'Done')      AS done_issues,
    COALESCE(SUM(ci.story_points), 0)                                       AS total_sp,
    COALESCE(SUM(ci.story_points) FILTER (WHERE ci.status_category = 'Done'), 0) AS done_sp,
    ROUND(
        100.0 * COUNT(DISTINCT ci.key) FILTER (WHERE ci.status_category = 'Done')
        / NULLIF(COUNT(DISTINCT ci.key), 0), 1
    )                                                                       AS completion_pct
FROM deduped_links dl
JOIN issues i ON i.key = dl.from_key
LEFT JOIN issues ci
    ON  ci.epic_key    = dl.from_key
    AND ci.issue_type NOT IN ('Epic', 'Sub-task')
GROUP BY dl.to_key, dl.from_key;

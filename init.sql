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
    goal           TEXT,
    synced_at      TIMESTAMPTZ DEFAULT NOW()
);

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
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);

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

-- Sprint snapshots for planning-deviation tracking
-- Taken at sprint start and sprint close
CREATE TABLE IF NOT EXISTS sprint_snapshots (
    sprint_id                INTEGER REFERENCES sprints(id),
    snapshot_type            TEXT,  -- 'start', 'close'
    snapshot_at              TIMESTAMPTZ DEFAULT NOW(),
    total_issues             INTEGER,
    total_story_points       NUMERIC,
    completed_issues         INTEGER,
    completed_story_points   NUMERIC,
    PRIMARY KEY (sprint_id, snapshot_type)
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

-- Planning deviation per sprint (committed vs delivered story points)
CREATE OR REPLACE VIEW v_planning_deviation AS
SELECT
    s.id                                                            AS sprint_id,
    s.name                                                          AS sprint_name,
    s.state,
    s.start_date,
    s.complete_date,
    start_snap.total_story_points                                   AS committed_points,
    close_snap.completed_story_points                               AS delivered_points,
    close_snap.completed_story_points - start_snap.total_story_points AS deviation_points,
    CASE
        WHEN start_snap.total_story_points > 0
        THEN ROUND(
            100.0 * close_snap.completed_story_points / start_snap.total_story_points,
            1
        )
        ELSE NULL
    END                                                             AS delivery_pct
FROM sprints s
LEFT JOIN sprint_snapshots start_snap
    ON start_snap.sprint_id = s.id AND start_snap.snapshot_type = 'start'
LEFT JOIN sprint_snapshots close_snap
    ON close_snap.sprint_id = s.id AND close_snap.snapshot_type = 'close';

-- Factory state. Content (the task packets) lives in git under tasks/; this
-- database holds only what changes as the queue runs: status, attempts, review
-- findings, cost. See factory/README.md for why the two are split.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per packet in tasks/. Seeded by `run.py sync`, which reads the
-- frontmatter; everything below `status` is owned by the loop.
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,          -- 'B01'
    slug          TEXT NOT NULL,
    packet_path   TEXT NOT NULL,             -- repo-relative
    packet_sha256 TEXT NOT NULL,             -- detects an edited packet
    spec_commit   TEXT NOT NULL,             -- spec sha the packet was cut from
    spec_path     TEXT NOT NULL,             -- which spec to drift-check against
    -- Capability *tiers*, not model names: 'basic' | 'standard' | 'advanced'.
    -- The column names predate the tier vocabulary and are kept because
    -- `db._migrate` can only add a column, never rename one.
    model         TEXT NOT NULL,             -- implementer tier
    reviewer      TEXT NOT NULL,             -- always >= model, minimum 'standard'
    gate          TEXT NOT NULL DEFAULT 'auto',   -- 'auto' | 'human'
    needs_db      INTEGER NOT NULL DEFAULT 0,
    surface       TEXT NOT NULL DEFAULT 'api',    -- 'api' | 'webapp'
    goal          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'ready',
    blocked_reason TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    merged_sha    TEXT,
    ordinal       INTEGER NOT NULL DEFAULT 0,  -- tie-break within ready set
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK (status IN ('ready','running','gating','reviewing','awaiting_human',
                      'merging','needs_work','blocked','done')),
    CHECK (gate IN ('auto','human'))
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on)
);

-- One row per implementation run. `agent_status` is what the model claimed;
-- `gate_status` is what the harness measured. They are stored separately on
-- purpose — a disagreement between them is itself a finding.
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no   INTEGER NOT NULL,
    -- The concrete model that ran, e.g. 'haiku' — NOT the tier stored in
    -- `tasks.model`. The cost and turn counts on this row were measured on
    -- this model, and repointing a tier later must not silently rewrite what
    -- past attempts are understood to have been measured on.
    model        TEXT NOT NULL,
    branch       TEXT NOT NULL,
    base_sha     TEXT NOT NULL,
    head_sha     TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    agent_status TEXT,                      -- 'complete' | 'blocked' | 'failed'
    agent_json   TEXT,                      -- the validated result.json
    gate_status  TEXT,                      -- 'green' | 'red' | 'error'
    gate_json    TEXT,                      -- counts, baseline, per-gate detail
    cost_usd     REAL,
    -- How the worker authenticated. Under 'subscription' the CLI still reports
    -- a cost, computed from token counts and list prices, but nothing was
    -- billed — so the two kinds of number must never be added together.
    billing      TEXT NOT NULL DEFAULT 'api',
    duration_s   REAL,
    turns        INTEGER,
    run_dir      TEXT NOT NULL,
    UNIQUE (task_id, attempt_no)
);

-- Review feedback, kept across attempts: attempt N+1's prompt replays the
-- blockers from attempt N verbatim, and a task that exhausts its attempts
-- leaves a full record of how it failed.
CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY,
    attempt_id      INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    reviewer_model  TEXT NOT NULL,          -- concrete model, as in attempts.model
    verdict         TEXT NOT NULL,          -- as returned by the reviewer
    effective       TEXT NOT NULL,          -- after the harness overrides it
    override_reason TEXT,                   -- why accept was downgraded
    invariants_json TEXT NOT NULL,          -- per declared id: held/violated/unverifiable
    findings_json   TEXT NOT NULL,
    cost_usd        REAL,
    billing         TEXT NOT NULL DEFAULT 'api',
    created_at      TEXT NOT NULL,
    CHECK (verdict   IN ('accept','revise','reject')),
    CHECK (effective IN ('accept','revise','reject'))
);

-- Assessments of a packet, made before it was ever queued.
--
-- `task_id` is deliberately NOT a foreign key: a packet is assessed before
-- `sync` registers it, so at insert time there is usually no `tasks` row to
-- point at. That ordering is the whole point — an assessment that could only be
-- recorded for an already-queued task would be gating nothing.
--
-- This table is telemetry (which model, what it cost, when). The verdict that
-- actually gates `sync` is the JSON file in `tasks/assessments/`, because it has
-- to survive a machine change and this database does not.
CREATE TABLE IF NOT EXISTS assessments (
    id              INTEGER PRIMARY KEY,
    task_id         TEXT NOT NULL,
    packet_sha256   TEXT NOT NULL,      -- the bytes that were assessed
    assessor_model  TEXT NOT NULL,      -- concrete model, as in attempts.model
    verdict         TEXT NOT NULL,      -- as returned by the assessor
    effective       TEXT NOT NULL,      -- after the harness overrides it
    override_reason TEXT,               -- why ready was downgraded
    claims_json     TEXT NOT NULL,
    structural_json TEXT NOT NULL,
    findings_json   TEXT NOT NULL,
    cost_usd        REAL,
    billing         TEXT NOT NULL DEFAULT 'api',
    created_at      TEXT NOT NULL,
    CHECK (verdict   IN ('ready','recut','reject')),
    CHECK (effective IN ('ready','recut','reject'))
);

CREATE INDEX IF NOT EXISTS idx_assessments_packet
    ON assessments (task_id, packet_sha256);

-- Append-only. Everything the loop does, for post-mortem on a run that went
-- wrong in a way the structured tables did not anticipate.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    task_id TEXT,
    kind    TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_task ON events (task_id, id);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts (task_id, attempt_no);

-- A task is runnable when it is ready and every dependency has merged.
CREATE VIEW IF NOT EXISTS runnable AS
SELECT t.*
FROM tasks t
WHERE t.status IN ('ready', 'needs_work')
  AND NOT EXISTS (
      SELECT 1 FROM task_deps d
      LEFT JOIN tasks dep ON dep.id = d.depends_on
      WHERE d.task_id = t.id
        AND (dep.id IS NULL OR dep.status <> 'done')
  );

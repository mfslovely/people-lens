-- People Lens: database schema (PostgreSQL 16 + pgvector)
-- Apply with: psql "$DATABASE_URL" -f app/db/schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS hr;
CREATE SCHEMA IF NOT EXISTS kb;
CREATE SCHEMA IF NOT EXISTS agent;
CREATE SCHEMA IF NOT EXISTS ops;

-- ===================================================================
-- HR DOMAIN
-- ===================================================================
CREATE TYPE hr.employee_status   AS ENUM ('active','on_notice','inactive');
CREATE TYPE hr.position_status   AS ENUM ('filled','vacant','frozen');
CREATE TYPE hr.app_role          AS ENUM ('employee','manager','hr_admin');
CREATE TYPE hr.request_status    AS ENUM ('pending','approved','rejected','cancelled');
CREATE TYPE hr.approval_action   AS ENUM ('submitted','approved','rejected','escalated','cancelled');
CREATE TYPE hr.attendance_status AS ENUM ('present','absent','wfh','on_leave','holiday','half_day');
CREATE TYPE hr.request_channel   AS ENUM ('ui','agent','seed');

CREATE TABLE hr.departments (
    id               SERIAL PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    parent_id        INT REFERENCES hr.departments(id),
    head_employee_id INT   -- FK added after employees
);

CREATE TABLE hr.positions (
    id                SERIAL PRIMARY KEY,
    title             TEXT NOT NULL,
    department_id     INT NOT NULL REFERENCES hr.departments(id),
    grade             TEXT,
    assignment_status hr.position_status NOT NULL DEFAULT 'vacant',
    employee_id       INT   -- FK added after employees
);

CREATE TABLE hr.employees (
    id              SERIAL PRIMARY KEY,
    emp_code        TEXT NOT NULL UNIQUE,
    full_name       TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    department_id   INT NOT NULL REFERENCES hr.departments(id),
    position_id     INT REFERENCES hr.positions(id),
    designation     TEXT NOT NULL,
    manager_id      INT REFERENCES hr.employees(id),
    location        TEXT NOT NULL,
    joining_date    DATE NOT NULL,
    employment_type TEXT NOT NULL DEFAULT 'full_time',
    status          hr.employee_status NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (manager_id IS NULL OR manager_id <> id)
);
CREATE INDEX ON hr.employees (manager_id);
CREATE INDEX ON hr.employees (department_id, status);

ALTER TABLE hr.departments ADD FOREIGN KEY (head_employee_id) REFERENCES hr.employees(id);
ALTER TABLE hr.positions   ADD FOREIGN KEY (employee_id)      REFERENCES hr.employees(id);

CREATE TABLE hr.app_users (
    id          SERIAL PRIMARY KEY,
    employee_id INT NOT NULL UNIQUE REFERENCES hr.employees(id),
    role        hr.app_role NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE hr.leave_types (
    code                    TEXT PRIMARY KEY,          -- CL, SL, EL, WFH, LOP
    name                    TEXT NOT NULL,
    annual_quota            NUMERIC(5,1) NOT NULL,
    carry_forward_max       NUMERIC(5,1) NOT NULL DEFAULT 0,
    requires_doc_after_days INT,
    min_notice_days         INT NOT NULL DEFAULT 0,
    policy_doc_key          TEXT                       -- -> kb.documents.doc_key
);

CREATE TABLE hr.leave_balances (
    employee_id INT  NOT NULL REFERENCES hr.employees(id),
    leave_type  TEXT NOT NULL REFERENCES hr.leave_types(code),
    year        INT  NOT NULL,
    accrued     NUMERIC(5,1) NOT NULL DEFAULT 0,
    used        NUMERIC(5,1) NOT NULL DEFAULT 0,
    pending     NUMERIC(5,1) NOT NULL DEFAULT 0,
    available   NUMERIC(5,1) GENERATED ALWAYS AS (accrued - used - pending) STORED,
    PRIMARY KEY (employee_id, leave_type, year),
    CHECK (accrued - used - pending >= 0)
);

CREATE SEQUENCE hr.leave_request_seq;
CREATE TABLE hr.leave_requests (
    id              TEXT PRIMARY KEY
                    DEFAULT 'LR-' || to_char(now(),'YYYY') || '-' || lpad(nextval('hr.leave_request_seq')::text, 6, '0'),
    employee_id     INT  NOT NULL REFERENCES hr.employees(id),
    leave_type      TEXT NOT NULL REFERENCES hr.leave_types(code),
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    days            NUMERIC(4,1) NOT NULL CHECK (days > 0),
    half_day        BOOLEAN NOT NULL DEFAULT false,
    reason          TEXT,
    status          hr.request_status NOT NULL DEFAULT 'pending',
    approver_id     INT NOT NULL REFERENCES hr.employees(id),
    escalated_to    INT REFERENCES hr.employees(id),
    idempotency_key TEXT UNIQUE,
    created_via     hr.request_channel NOT NULL DEFAULT 'agent',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ,
    CHECK (end_date >= start_date),
    CHECK (approver_id <> employee_id),
    -- Hard guarantee: no overlapping active leave per employee
    EXCLUDE USING gist (
        employee_id WITH =,
        daterange(start_date, end_date, '[]') WITH &&
    ) WHERE (status IN ('pending','approved'))
);
CREATE INDEX ON hr.leave_requests (approver_id, status);
CREATE INDEX ON hr.leave_requests (start_date, end_date);

CREATE TABLE hr.approval_history (
    id         BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES hr.leave_requests(id),
    actor_id   INT  NOT NULL REFERENCES hr.employees(id),
    action     hr.approval_action NOT NULL,
    comment    TEXT,
    acted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id   TEXT
);
CREATE INDEX ON hr.approval_history (request_id);

CREATE TABLE hr.attendance (
    employee_id INT  NOT NULL REFERENCES hr.employees(id),
    date        DATE NOT NULL,
    status      hr.attendance_status NOT NULL,
    check_in    TIME,
    check_out   TIME,
    PRIMARY KEY (employee_id, date)
);
CREATE INDEX ON hr.attendance (date, status);

CREATE TABLE hr.holidays (
    date     DATE NOT NULL,
    name     TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT 'ALL',
    PRIMARY KEY (date, location)
);

-- In-app notifications (new approval requests for approvers, decisions for requesters)
CREATE TABLE hr.notifications (
    id           BIGSERIAL PRIMARY KEY,
    recipient_id INT  NOT NULL REFERENCES hr.employees(id),
    kind         TEXT NOT NULL,           -- approval_needed | leave_submitted | leave_approved | leave_rejected
    title        TEXT NOT NULL,
    body         TEXT,
    request_id   TEXT REFERENCES hr.leave_requests(id),
    is_read      BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON hr.notifications (recipient_id, is_read, created_at DESC);

-- Reporting tree: every (manager, report, depth) pair, direct and indirect
CREATE VIEW hr.v_reporting_tree AS
WITH RECURSIVE tree AS (
    SELECT manager_id, id AS employee_id, 1 AS depth
      FROM hr.employees WHERE manager_id IS NOT NULL
    UNION ALL
    SELECT t.manager_id, e.id, t.depth + 1
      FROM tree t JOIN hr.employees e ON e.manager_id = t.employee_id
     WHERE t.depth < 10
)
SELECT * FROM tree;

-- ===================================================================
-- SCOPED VIEWS (the only objects text-to-SQL may read)
-- Session variables are set per transaction by the tool executor:
--   SET LOCAL app.employee_id = '<id>'; SET LOCAL app.role = '<role>';
-- ===================================================================
-- SECURITY DEFINER: the read-only analytics role cannot read base tables, but it can still resolve its scope.
-- The scope comes only from session settings, which the tool executor sets from the verified principal.
CREATE OR REPLACE FUNCTION hr.visible_employee_ids() RETURNS SETOF INT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = hr, pg_temp AS $$
    SELECT id FROM hr.employees
     WHERE current_setting('app.role', true) = 'hr_admin'
    UNION
    SELECT current_setting('app.employee_id', true)::int
    UNION
    SELECT employee_id FROM hr.v_reporting_tree
     WHERE current_setting('app.role', true) = 'manager'
       AND manager_id = current_setting('app.employee_id', true)::int
$$;

CREATE VIEW hr.v_employees_scoped AS
SELECT e.id, e.emp_code, e.full_name, d.name AS department, e.designation,
       e.manager_id, e.location, e.joining_date, e.status
  FROM hr.employees e JOIN hr.departments d ON d.id = e.department_id
 WHERE e.id IN (SELECT hr.visible_employee_ids());

CREATE VIEW hr.v_leave_scoped AS
SELECT r.id, r.employee_id, r.leave_type, r.start_date, r.end_date, r.days,
       r.status, r.approver_id, r.created_at, r.decided_at
  FROM hr.leave_requests r
 WHERE r.employee_id IN (SELECT hr.visible_employee_ids());

CREATE VIEW hr.v_balances_scoped AS
SELECT b.* FROM hr.leave_balances b
 WHERE b.employee_id IN (SELECT hr.visible_employee_ids());

CREATE VIEW hr.v_attendance_scoped AS
SELECT a.* FROM hr.attendance a
 WHERE a.employee_id IN (SELECT hr.visible_employee_ids());

CREATE VIEW hr.v_positions AS
SELECT p.id, p.title, d.name AS department, p.grade, p.assignment_status
  FROM hr.positions p JOIN hr.departments d ON d.id = p.department_id;

-- Read-only role used by the analytics tool. It can only see the scoped views.
DO $$ BEGIN
    CREATE ROLE pl_readonly NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
GRANT USAGE ON SCHEMA hr TO pl_readonly;
GRANT SELECT ON hr.v_employees_scoped, hr.v_leave_scoped, hr.v_balances_scoped,
                hr.v_attendance_scoped, hr.v_positions TO pl_readonly;

-- ===================================================================
-- KNOWLEDGE BASE
-- ===================================================================
CREATE TABLE kb.documents (
    id       SERIAL PRIMARY KEY,
    doc_key  TEXT NOT NULL UNIQUE,     -- 'leave_policy', 'wfh_policy', ...
    title    TEXT NOT NULL,
    category TEXT NOT NULL,
    owner    TEXT
);

CREATE TABLE kb.document_versions (
    id             SERIAL PRIMARY KEY,
    document_id    INT  NOT NULL REFERENCES kb.documents(id),
    version        TEXT NOT NULL,
    effective_from DATE NOT NULL,
    effective_to   DATE,
    is_current     BOOLEAN NOT NULL DEFAULT false,
    source_path    TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version),
    UNIQUE (document_id, sha256)       -- re-ingesting identical content is a no-op
);
CREATE UNIQUE INDEX one_current_version ON kb.document_versions (document_id) WHERE is_current;

CREATE TABLE kb.chunks (
    id                BIGSERIAL PRIMARY KEY,
    version_id        INT  NOT NULL REFERENCES kb.document_versions(id) ON DELETE CASCADE,
    ordinal           INT  NOT NULL,
    section_path      TEXT NOT NULL,   -- 'Leave Policy > 4. Earned Leave > 4.2 Carry forward'
    content           TEXT NOT NULL,
    token_count       INT  NOT NULL,
    embedding         VECTOR(384) NOT NULL,   -- bge-small-en-v1.5
    tsv               TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', section_path || ' ' || content)) STORED,
    metadata          JSONB NOT NULL DEFAULT '{}',
    flagged_injection BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (version_id, ordinal)
);
CREATE INDEX chunks_embedding_hnsw ON kb.chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_gin        ON kb.chunks USING gin (tsv);

-- ===================================================================
-- AGENT RUNTIME
-- ===================================================================
CREATE TYPE agent.msg_role      AS ENUM ('user','assistant','tool');
CREATE TYPE agent.action_status AS ENUM ('proposed','confirmed','executed','cancelled','expired','failed');

CREATE TABLE agent.sessions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        INT NOT NULL REFERENCES hr.app_users(id),
    role_at_start  hr.app_role NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON agent.sessions (user_id);

CREATE TABLE agent.messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES agent.sessions(id) ON DELETE CASCADE,
    turn       INT  NOT NULL,
    role       agent.msg_role NOT NULL,
    content    TEXT NOT NULL,
    tool_name  TEXT,
    citations  JSONB,
    trace_id   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON agent.messages (session_id, turn);

-- Rolling summary + resolved entities, used for references like "these" or "that period"
CREATE TABLE agent.session_state (
    session_id UUID PRIMARY KEY REFERENCES agent.sessions(id) ON DELETE CASCADE,
    summary    TEXT,
    entities   JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LangGraph checkpoint tables are created by PostgresSaver.setup()

CREATE TABLE agent.pending_actions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES agent.sessions(id),
    user_id         INT  NOT NULL REFERENCES hr.app_users(id),
    action_type     TEXT NOT NULL CHECK (action_type IN ('submit_leave','approve_request','reject_request')),
    payload         JSONB NOT NULL,
    validation      JSONB NOT NULL,
    status          agent.action_status NOT NULL DEFAULT 'proposed',
    idempotency_key TEXT NOT NULL UNIQUE,
    result_ref      TEXT,               -- e.g. leave_requests.id
    error           TEXT,
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT now() + interval '15 minutes',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    executed_at     TIMESTAMPTZ
);
CREATE INDEX ON agent.pending_actions (session_id, status);

-- ===================================================================
-- OBSERVABILITY & EVAL
-- ===================================================================
CREATE TABLE ops.traces (
    trace_id      TEXT PRIMARY KEY,
    session_id    UUID,
    user_id       INT,
    role          hr.app_role,
    intents       TEXT[],
    model         TEXT,
    input_tokens  INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    cost_usd      NUMERIC(10,6) DEFAULT 0,
    latency_ms    INT,
    status        TEXT NOT NULL,          -- ok | blocked | error | degraded
    error_code    TEXT,
    fallback_used BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON ops.traces (created_at);

CREATE TABLE ops.tool_calls (
    id                  BIGSERIAL PRIMARY KEY,
    trace_id            TEXT NOT NULL REFERENCES ops.traces(trace_id),
    tool_name           TEXT NOT NULL,
    args_redacted       JSONB,
    authz_decision      TEXT NOT NULL,    -- allow | deny:<reason>
    status              TEXT NOT NULL,    -- ok | error | denied
    latency_ms          INT,
    retries             INT NOT NULL DEFAULT 0,
    error               TEXT,
    retrieved_chunk_ids BIGINT[],
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON ops.tool_calls (trace_id);

CREATE TABLE ops.audit_log (
    id         BIGSERIAL PRIMARY KEY,
    trace_id   TEXT,
    actor_id   INT,
    event      TEXT NOT NULL,             -- authz_denied | injection_blocked | action_executed | ...
    detail     JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ops.eval_runs (
    id         SERIAL PRIMARY KEY,
    git_sha    TEXT,
    model      TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metrics    JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE ops.eval_results (
    run_id        INT  NOT NULL REFERENCES ops.eval_runs(id) ON DELETE CASCADE,
    case_id       TEXT NOT NULL,
    category      TEXT NOT NULL,
    passed        BOOLEAN NOT NULL,
    actual_output TEXT,
    checks        JSONB NOT NULL DEFAULT '{}',
    latency_ms    INT,
    tokens        INT,
    PRIMARY KEY (run_id, case_id)
);

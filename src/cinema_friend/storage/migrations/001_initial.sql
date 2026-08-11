CREATE TABLE conversation_drafts (
  user_id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE watches (
  id TEXT PRIMARY KEY,
  owner_user_id INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  slug TEXT NOT NULL,
  title TEXT,
  criteria_json TEXT NOT NULL,
  mode TEXT NOT NULL,
  interval_seconds INTEGER,
  status TEXT NOT NULL,
  next_run_at TEXT,
  last_check_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX watches_due_idx ON watches(status, next_run_at);
CREATE INDEX watches_owner_idx ON watches(owner_user_id, created_at);

CREATE TABLE check_runs (
  id TEXT PRIMARY KEY,
  watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL,
  outcome TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  performance_count INTEGER NOT NULL DEFAULT 0,
  option_count INTEGER NOT NULL DEFAULT 0,
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE result_snapshots (
  id TEXT PRIMARY KEY,
  watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  check_run_id TEXT UNIQUE REFERENCES check_runs(id) ON DELETE SET NULL,
  checked_at TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  is_latest INTEGER NOT NULL CHECK (is_latest IN (0, 1))
);
CREATE UNIQUE INDEX one_latest_snapshot_idx
  ON result_snapshots(watch_id) WHERE is_latest = 1;

CREATE TABLE result_options (
  snapshot_id TEXT NOT NULL REFERENCES result_snapshots(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  option_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, rank),
  UNIQUE(snapshot_id, option_key)
);

CREATE TABLE notified_options (
  watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  option_key TEXT NOT NULL,
  first_notified_at TEXT NOT NULL,
  PRIMARY KEY(watch_id, option_key)
);

CREATE TABLE notification_state (
  watch_id TEXT PRIMARY KEY REFERENCES watches(id) ON DELETE CASCADE,
  last_best_rank_json TEXT,
  degradation_notified INTEGER NOT NULL DEFAULT 0 CHECK (degradation_notified IN (0, 1)),
  recovery_pending INTEGER NOT NULL DEFAULT 0 CHECK (recovery_pending IN (0, 1)),
  updated_at TEXT NOT NULL
);

CREATE TABLE notification_deliveries (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  recipient_user_id INTEGER NOT NULL,
  watch_id TEXT REFERENCES watches(id) ON DELETE CASCADE,
  snapshot_id TEXT REFERENCES result_snapshots(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE INDEX notification_due_idx ON notification_deliveries(status, next_attempt_at);

CREATE TABLE host_circuits (
  host TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  step INTEGER NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  next_probe_at TEXT,
  updated_at TEXT NOT NULL
);

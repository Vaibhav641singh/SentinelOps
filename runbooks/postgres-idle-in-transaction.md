# Postgres sessions stuck idle in transaction

service: postgres
component: backend
severity: high
version_context: PostgreSQL 14-18; transaction_timeout requires 17 or newer
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
Sessions sit in state idle in transaction for minutes or hours, holding both a
connection slot and a transaction snapshot.

Visible as:
state = 'idle in transaction' in pg_stat_activity, with xact_start far in the past
WARNING: oldest xmin is far in the past
ERROR: canceling statement due to conflict with recovery on hot standbys

Tables that are being written continuously grow on disk while autovacuum runs
and reports nothing reclaimed. Lock waits appear on unrelated DDL.

## Impact
Three separate failures with one cause. Connection slots are consumed, leading
to max_connections exhaustion. Vacuum cannot remove dead tuples newer than the
oldest open snapshot, so tables and indexes bloat and queries slow down. Any
ALTER TABLE queues behind the open transaction's locks and blocks every
session arriving after it.

## Diagnosis
Find the sessions and how long they have been open:

  SELECT pid, usename, application_name, state,
         now() - xact_start AS txn_age,
         now() - state_change AS idle_for, query
  FROM pg_stat_activity
  WHERE state LIKE 'idle in transaction%'
  ORDER BY txn_age DESC;

Confirm vacuum is actually being held back:

  SELECT max(age(backend_xmin)) FROM pg_stat_activity;
  SELECT relname, n_dead_tup, last_autovacuum FROM pg_stat_user_tables
  ORDER BY n_dead_tup DESC LIMIT 10;

Check what the transaction is blocking:

  SELECT pid, pg_blocking_pids(pid), wait_event_type, query
  FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;

The query column shows the last statement run, not a running one. It is a clue
to which code path left the transaction open, not evidence of active work.

## Remediation
1. Terminate transactions older than the agreed threshold:
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE state LIKE 'idle in transaction%' AND now() - xact_start > interval '10 minutes';
2. Set a server-side limit so this self-corrects:
   ALTER SYSTEM SET idle_in_transaction_session_timeout = '60s';
   SELECT pg_reload_conf();
3. Fix the application path identified by application_name: commit or roll back
   before any network call, and avoid opening a transaction to run reads.
4. Once the oldest snapshot is released, let autovacuum catch up, or run
   VACUUM ANALYZE on the worst-bloated tables.

## Prevention
Keep idle_in_transaction_session_timeout set in postgresql.conf, not only per
role. On PostgreSQL 17 and newer, transaction_timeout additionally caps
transactions that stay busy rather than idle.

Alert on max(age(backend_xmin)) crossing a few million, which catches the
bloat cause before the disk symptom.

# Postgres max_connections reached

service: postgres
component: postmaster
severity: critical
version_context: PostgreSQL 14-18 on Linux
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
New connections are refused outright while sessions already established keep
working normally.

Server log and client output:
FATAL: sorry, too many clients already
FATAL: remaining connection slots are reserved for non-replication superuser connections

The second string appears first: superuser_reserved_connections holds back the
last few slots, so ordinary roles are refused before the server is truly full.

Metrics: pg_stat_activity row count flat against the max_connections setting,
and a rising rate of connection errors at the application.

## Impact
Total loss of new work. Every application instance that restarts or scales up
fails to connect and stays down. Monitoring and backup jobs that open their own
connection fail at the same time, which is why this usually surfaces as several
unrelated alerts at once.

## Diagnosis
Connect using a superuser role, which still has reserved slots available:

  sudo -u postgres psql

Then:

  SHOW max_connections;
  SHOW superuser_reserved_connections;
  SELECT count(*) FROM pg_stat_activity;
  SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;
  SELECT usename, application_name, count(*) FROM pg_stat_activity
    GROUP BY 1, 2 ORDER BY 3 DESC;

The grouped counts answer the real question: whether one client is leaking
connections or load genuinely grew.

## Remediation
1. Identify sessions safe to drop, usually idle or idle in transaction:
   SELECT pid, usename, state, now() - state_change AS idle_for, query
   FROM pg_stat_activity WHERE state LIKE 'idle%' ORDER BY idle_for DESC;
2. Terminate the worst offenders:
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE state = 'idle in transaction' AND now() - state_change > interval '5 minutes';
3. If one application is leaking, stop or roll back that deployment. Raising
   the limit under a leak only moves the failure later.
4. For lasting headroom, raise max_connections in postgresql.conf. This
   requires a restart: SELECT pg_reload_conf() will not apply it.
5. Restart during a maintenance window:
   sudo systemctl restart postgresql
6. Where the true client count is high, put PgBouncer in front rather than
   raising max_connections further. Each backend is a separate OS process and
   costs memory whether busy or idle.

## Prevention
Set idle_in_transaction_session_timeout = '60s' so a forgotten transaction
releases its slot on its own.

Alert when pg_stat_activity count exceeds 80 percent of max_connections, which
gives time to act before connections are refused.

Pin an explicit maximum pool size in every application and keep the sum of
those maxima below max_connections.

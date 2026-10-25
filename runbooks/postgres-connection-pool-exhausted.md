# Postgres connection pool exhausted

service: postgres
component: pgbouncer
severity: high
version_context: PgBouncer 1.18-1.24 fronting PostgreSQL 14-18
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
Clients block and then time out while the database itself looks idle.

PgBouncer log:
FATAL: no more connections allowed (max_client_conn)

Application logs:
HikariPool-1 - Connection is not available, request timed out after 30000ms
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00

In SHOW POOLS the cl_waiting column is non-zero and maxwait climbs above 0.
The Postgres backend count stays flat at default_pool_size. That flatness is
the tell: the ceiling is the pooler, not the server.

## Impact
Every request that needs a database connection queues behind a free server
slot and eventually times out. Read and write paths fail together. Health
checks that touch the database fail, so instances are removed from rotation
while the database is still accepting work.

## Diagnosis
Connect to the admin console:

  psql -h 127.0.0.1 -p 6432 -U pgbouncer pgbouncer

Then, in order:

  SHOW POOLS;
  SHOW CLIENTS;
  SHOW CONFIG;

Compare cl_active plus cl_waiting against max_client_conn, and sv_active
against default_pool_size. Whichever is pinned is the binding limit.

On the database, confirm the server side is not the constraint:

  SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
  SHOW max_connections;

## Remediation
1. Terminate sessions holding a server connection without using it:
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE state = 'idle in transaction' AND now() - state_change > interval '5 minutes';
2. If pool_mode is session, set pool_mode = transaction in pgbouncer.ini so a
   server connection returns to the pool at commit rather than at disconnect.
3. Raise default_pool_size, and raise max_client_conn if cl_waiting was the
   pinned value.
4. Apply without dropping clients by running RELOAD; in the admin console.
5. Cap the application pool so the sum of all instance maxima cannot exceed
   max_client_conn.
6. Re-check SHOW POOLS; maxwait should return to 0.

## Prevention
Set server_idle_timeout = 600 and query_wait_timeout = 120 in pgbouncer.ini so
a stuck client is reclaimed instead of held forever.

Set idle_in_transaction_session_timeout = '60s' on the application role.

Size the tiers so that default_pool_size multiplied by the number of pools
stays below max_connections minus superuser_reserved_connections.

Alert on maxwait greater than 0 sustained for one minute, and on cl_waiting
greater than 0. Both lead the user-visible timeout by several seconds.

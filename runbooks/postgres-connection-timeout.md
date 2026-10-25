# Postgres connection timeout

service: postgres
component: network
severity: high
version_context: PostgreSQL 14-18, libpq clients, Linux hosts
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
The connection attempt hangs for the full timeout and then fails. Nothing is
returned quickly.

psql: error: connection to server at "db.internal" (10.0.1.5), port 5432 failed: timeout expired
java.net.SocketTimeoutException: connect timed out
psycopg.OperationalError: connection timeout expired

Distinguish this from connection refused. Refused is an immediate TCP reset and
means nothing is listening. A timeout means packets are being dropped in
flight, or the server is accepting the socket but too busy to complete the
handshake. The two have different causes and different fixes.

## Impact
Worse than an outright refusal, because clients hold a worker or thread for the
whole timeout before failing. Thread pools and request queues back up, so one
database problem turns into a wider application stall.

## Diagnosis
Confirm where it stalls:

  time pg_isready -h db.internal -p 5432 -t 5
  nc -zv db.internal 5432

If nc hangs rather than reporting refused, packets are being dropped. Check the
path and the filtering rules:

  sudo traceroute -T -p 5432 db.internal
  sudo iptables -L -n | grep 5432

In a cloud network, check the security group or firewall rule for the client
subnet. A rule that was removed produces exactly this behaviour, because the
default action is to drop rather than reject.

If the network path is clean, look at the server. A server in crash recovery
accepts the socket but refuses to finish the handshake until recovery ends:

  sudo tail -50 /var/log/postgresql/postgresql-16-main.log

Look for "database system is starting up" or redo progress lines. Also check
whether the listen backlog is saturated:

  ss -ltn 'sport = :5432'

A large Recv-Q against a small Send-Q on the listening socket means connections
are arriving faster than the postmaster can fork backends.

## Remediation
1. If a firewall or security group rule is missing, restore the rule allowing
   the client subnet to reach port 5432, then retest with nc.
2. If the server is in recovery, wait for it to finish. Do not restart, which
   restarts recovery from the beginning.
3. If the backlog is saturated, reduce the connection rate by putting PgBouncer
   in front, so clients queue at the pooler instead of at the postmaster.
4. Set an explicit client connect timeout so a stall fails fast rather than
   holding a worker: connect_timeout=5 in the libpq connection string.
5. Retest end to end with pg_isready before declaring recovery.

## Prevention
Set connect_timeout on every client, and enable TCP keepalives with
tcp_keepalives_idle so a silently dropped connection is detected rather than
hanging until the OS gives up.

Run a synthetic connection probe from the application network, not from the
database host. A probe on the server never crosses the firewall that fails.

Alert on connection latency, not only on connection errors. Timeouts show up as
rising latency well before the first failure.

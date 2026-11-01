# Postgres connection refused

service: postgres
component: listener
severity: critical
version_context: PostgreSQL 14-18 on Linux with systemd
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
The connection attempt fails immediately rather than hanging.

psql: error: connection to server at "db.internal" (10.0.1.5), port 5432 failed: Connection refused
	Is the server running on that host and accepting TCP/IP connections?

org.postgresql.util.PSQLException: Connection to db.internal:5432 refused.
psycopg.OperationalError: connection failed: Connection refused

Speed is the diagnostic signal. Refused means the host answered with a TCP
reset, so something is reachable and nothing is listening on that port. A
firewall that drops packets produces a timeout instead, not this.

## Impact
Complete outage for every client of that instance. Unlike a saturated server,
no session survives, because the process accepting connections is gone or
bound to an address the clients cannot reach.

## Diagnosis
Check whether the server is running at all:

  systemctl status postgresql
  pg_isready -h db.internal -p 5432

Check what is listening, and on which address:

  sudo ss -ltnp | grep 5432

A socket bound to 127.0.0.1:5432 rather than 0.0.0.0:5432 explains a refusal
that only remote clients see. Confirm the configured value:

  sudo -u postgres psql -c "SHOW listen_addresses;"
  sudo -u postgres psql -c "SHOW port;"

If nothing is listening, the server either failed to start or exited. Read the
end of the log:

  sudo tail -100 /var/log/postgresql/postgresql-16-main.log
  journalctl -u postgresql --since "30 min ago"

Common startup failures recorded there are a full data directory, a corrupt or
stale postmaster.pid, and a port already in use.

## Remediation
1. If the service is stopped, start it and confirm it stays up:
   sudo systemctl start postgresql
   systemctl status postgresql
2. If it exits on start, fix the cause named in the log before retrying. Do not
   delete postmaster.pid while a postgres process is still running.
3. If it is bound to localhost only, set listen_addresses in postgresql.conf to
   the interface clients use, or to '*' behind a trusted network boundary.
4. Restart, since listen_addresses is not reloadable:
   sudo systemctl restart postgresql
5. Add the matching pg_hba.conf line for the client network, then apply it:
   sudo -u postgres psql -c "SELECT pg_reload_conf();"
6. Verify from a client host:
   pg_isready -h db.internal -p 5432

## Prevention
Run pg_isready as the service health check so a failed start pages on its own
rather than through client errors.

Keep listen_addresses, port and pg_hba.conf under configuration management, so
a manual edit cannot survive a reboot unnoticed.

Alert on the systemd unit leaving active state, and monitor data directory free
space, which is the most common reason the server refuses to start again.

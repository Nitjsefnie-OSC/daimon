#!/usr/bin/env bash
set -euo pipefail

psql -h localhost -U daimon -d daimon_test -v ON_ERROR_STOP=1 <<'SQL'
SELECT pid, application_name, state, now() - xact_start AS transaction_age,
       wait_event_type, wait_event, pg_blocking_pids(pid) AS blocking_pids,
       left(query, 500) AS query
FROM pg_stat_activity
WHERE datname = current_database()
ORDER BY pid;
SELECT l.pid, a.application_name, l.locktype, l.mode, l.granted,
       n.nspname AS schema, c.relname AS relation, l.transactionid
FROM pg_locks AS l
LEFT JOIN pg_stat_activity AS a ON a.pid = l.pid
LEFT JOIN pg_class AS c ON c.oid = l.relation
LEFT JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
   OR l.database IS NULL
ORDER BY l.granted, l.pid, l.locktype, l.mode;
SQL

# Deploying massingplan

## The short version

```bash
docker compose up --build
```

Then `http://localhost:8000`. Create the first owner:

```bash
MASSINGPLAN_ADMIN_PASSWORD='a-long-enough-passphrase' docker compose exec app massingplan create-admin you@example.com
```

Before you expose that to anything, read the next section.

## Before you expose it

**Set `MASSINGPLAN_SECRET_KEY`.** The container refuses to start in production
without it, on purpose. Four workers each generating their own key invalidate
each other's sessions, and the symptom — users logged out at random — is a long
way from the cause.

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**Change the compose Postgres password.** It is `massingplan/massingplan` in the
file, spelled out rather than hidden so you can see what you are running.

**Terminate TLS in front.** The app sets HSTS only in production and only
assumes it is behind something; it does not terminate TLS itself.

**Set `FORWARDED_ALLOW_IPS` to your load balancer.** It defaults to
`127.0.0.1`. Setting it to `*` lets any client spoof its own source address,
which is what the audit log records.

**Put a rate limit at your ingress.** Account lock-out covers credential
stuffing. It does not cover a scripted flood of Monte Carlo requests, and there
is no in-app rate limiter yet — see `SECURITY.md`.

## Configuration

Everything is read from the environment with a `MASSINGPLAN_` prefix.

| Variable | Default | Notes |
|---|---|---|
| `MASSINGPLAN_ENV` | `development` | `production` enables secure cookies, HSTS and JSON logs, and makes the secret key mandatory |
| `MASSINGPLAN_SECRET_KEY` | *(generated in dev)* | Required in production. Rotating it signs everyone out, which is the intended effect |
| `MASSINGPLAN_DATABASE_URL` | `sqlite:///instance/massingplan.db` | `postgresql+psycopg://…` in production |
| `MASSINGPLAN_LOG_LEVEL` | `INFO` | |
| `MASSINGPLAN_MAX_UPLOAD_BYTES` | `16777216` | A large XER is legitimately several MB |
| `MASSINGPLAN_SESSION_LIFETIME` | `43200` | Seconds. Twelve hours |
| `MASSINGPLAN_SKIP_MIGRATIONS` | `0` | Set to `1` on every replica but one |
| `WEB_CONCURRENCY` | `2 × cores + 1` | Bounded by your database's connection limit, not by this process |
| `WEB_THREADS` | `4` | |
| `WEB_TIMEOUT` | `120` | A 2,000-iteration Monte Carlo is CPU-bound and slow on purpose |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Your load balancer's address |

## Migrations

The entrypoint runs `alembic upgrade head` and retries up to fifteen times.
Postgres accepts TCP connections several seconds before it will serve a query,
so a single attempt at container start loses the race about one boot in five and
the container restart-loops.

**Run it from one place.** With several replicas, set
`MASSINGPLAN_SKIP_MIGRATIONS=1` on all but one, or run migrations as a separate
job before the rollout. Concurrent `alembic upgrade` against one database is a
race with no winner.

Rolling back:

```bash
docker compose exec app alembic downgrade -1
```

Every migration in this repo has been tested up, down and up again, on both
SQLite and Postgres, by the `migrations` and `postgres` CI jobs.

## Probes

- **`/healthz`** — liveness. Deliberately does *not* touch the database. A
  liveness probe that fails during a brief database blip kills a container that
  would have recovered, which does not bring the database back and does lose
  in-flight requests.
- **`/readyz`** — readiness. Schedules a one-activity network and issues
  `SELECT 1`. Both are checked for real; returning `ok` without checking is how
  a container reports itself healthy for its entire life while no traffic works.

Kubernetes:

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  periodSeconds: 10
```

## Logs

JSON, one object per line, on stdout. Every response carries `X-Request-Id`, and
the id appears in the log line — so a user reporting "it broke at 14:32" can
hand over an identifier that finds the exact request rather than a timestamp
that finds four hundred.

Passwords, keys, cookies and authorization headers are redacted by key name. A
log carrying the credential whose use it recorded is a second copy of the
credential.

## Backups

`pg_dump` the database. There is nothing else: uploads are parsed and stored as
rows, not kept as files, so the database is the whole state.

**On SQLite, the database is three files.** WAL mode is on, so committed data
lives in `massingplan.db-wal` until a checkpoint. Copying only `massingplan.db`
gives you a backup that is silently missing recent writes — and deleting only
`massingplan.db` leaves a `-wal` that resurrects the state you thought you had
removed. Use `sqlite3 massingplan.db ".backup out.db"`, or stop the app and copy
all three.

Verify a restore, not just a dump. To check a restored copy is a working
system rather than a working file:

```bash
MASSINGPLAN_DATABASE_URL=postgresql+psycopg://…/restored massingplan check
```

## Scaling

The app is stateless apart from the session cookie, so replicas scale
horizontally. The bound is the database connection pool: each worker thread can
hold a connection, so `WEB_CONCURRENCY × WEB_THREADS × replicas` must stay under
your Postgres `max_connections`.

Scheduling is CPU-bound. A 2,000-activity network takes tens of milliseconds; a
2,000-iteration Monte Carlo over it takes tens of seconds. If risk runs become
common, move them to a queue rather than raising `WEB_TIMEOUT` — a request that
holds a worker for a minute is a worker that is not serving pages.

## Upgrading

1. Read `CHANGELOG.md`, particularly **Fixed**. Two entries there change
   computed output.
2. Back up.
3. Deploy with `MASSINGPLAN_SKIP_MIGRATIONS=1`, run the migration as a job, then
   let the replicas roll.
4. Check `/readyz` returns `ready` rather than merely 200.

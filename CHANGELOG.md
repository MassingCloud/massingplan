# Changelog

Keep a Changelog, Semantic Versioning. Grouped by the phase that produced it.

## [Unreleased]

### Added — the engine

- Multi-calendar CPM on an absolute ordinal time axis with half-open spans. All
  four relationship types, leads and lags with an explicit lag calendar, all ten
  constraint types split into soft and mandatory.
- Data date, actual dates, remaining duration as a first-class field, and the
  retained-logic versus progress-override switch — implemented as a link-set
  transform rather than a flag threaded through the forward pass.
- DCMA 14-point assessment with tri-state results: a check that cannot run is
  *skipped* and leaves the score's denominator.
- Primavera XER and MS Project MSPDI, read and write, with no third-party
  dependency — including `TASKPRED`, `SCHEDOPTIONS` and real calendar
  exceptions.
- Monte Carlo risk, deterministic serial-SGS resource levelling, and
  baseline-to-baseline delay attribution whose contributions sum exactly to the
  finish move.
- `core/timeaxis.py` at 100% branch coverage with an exhaustive sweep.

### Added — the product

- Flask app with a self-hosted SVG Gantt: dependency arrows, float tails,
  driving-path highlight, milestone diamonds. No CDN; `default-src 'self'`.
- Persistence: SQLAlchemy 2.0 models, Alembic migrations, org-scoped queries
  that fail closed. Baselines stored as rows, not as a blob.
- Authentication, RBAC with four roles, API keys hashed at rest, an append-only
  audit log, and JSON logs with a request id echoed as `X-Request-Id`.
- `massingplan` CLI: `check`, `schedule`, `assess`, `demo`, `init-db`,
  `create-admin`, `seed-demo`.
- Docker image pinned by digest, non-root, with an entrypoint that retries
  migrations; compose with Postgres gated on a healthcheck.
- Postgres proven in CI, not assumed: migrations up-down-up, the dialect suite,
  and a step that fails if those tests silently skipped.

- Two-factor authentication: TOTP with an inline SVG QR, secrets encrypted at
  rest with Fernet, hashed single-use recovery codes, replay refused inside the
  drift window. Off unless `massingplan[mfa]` is installed and
  `MASSINGPLAN_ENCRYPTION_KEY` is set, in which case the page says so.
- Signed outbound webhooks, queued in the caller's transaction and delivered by
  `massingplan webhooks drain`. Every URL is vetted against its resolved
  addresses at subscribe time and again before each delivery; the connection is
  pinned to the address that was vetted.
- `tests/test_adversarial.py` and `tests/test_performance.py`, each with its own
  CI job — the first attacks the app from the outside, the second pins
  complexity rather than stopwatch times.

### Fixed — bugs the work surfaced

- **Self-service registration made strangers owners of the default
  organisation.** Registering without naming an organisation joined the existing
  default one as `OWNER`, so anybody who could reach the form could read every
  project in the tenant that holds seeded and imported work. Registration now
  always creates a new organisation; joining an existing one is by invitation.
  Found by `test_posting_a_role_field_does_not_grant_it`.
- **`/\evil.example.com` was an open redirect.** The guard was
  `startswith("/") and not startswith("//")`, and browsers normalise a backslash
  to a forward slash in the authority position — so that target passed the check
  and navigated off site. Both redirect sites now share `deps.safe_next`, which
  also refuses control characters and a colon in the first segment. Found by
  `test_sign_in_will_not_bounce_you_off_site`.
- **The JSON API accepted a session cookie.** It is exempt from CSRF on the
  grounds that a bearer key is never sent ambiently by a browser — but
  `current_principal()` also resolved from the session, which made every
  endpoint a CSRF-exempt, state-changing surface behind an ambient credential.
  The API now refuses cookie auth outright.
- **The project list still ran a full CPM per row.** `stored_summary` was
  written to fix this and the page never called it; calling it then loaded every
  activity of every project instead. The headline is now six denormalised
  columns on `projects`, written by `repository.write_back` and
  `repository.set_baseline`, and the list loads no children at all. Migration
  `0005` backfills existing rows — without that, every existing project would
  read "not scheduled yet" until somebody opened it.
- **`mfa` was referenced in `auth.sign_in` and never imported**, so every
  sign-in returned 500.

- **`ScheduleOutcome.data_date` reported the earliest early start**, not the
  date the schedule was computed from. DCMA checks 9, 11 and 14 all compare
  against it, and that derivation is correct only while nothing carries an
  actual — which is exactly when those checks become runnable. Check 14 reported
  *skipped* on a schedule that had a baseline and work due against it.
- **`DateTime(timezone=True)` is a lie on SQLite.** It returns naive datetimes,
  so the account lock-out comparison raised `TypeError` after a round trip
  through the database, and only then. The symptom would have been a 500 on the
  sign-in page for the users least able to work around it.
- **`calculate()` synthesised a default calendar and never told the caller**, so
  scheduling with no calendars raised inside the presenter. Caught by `/readyz`,
  which does its job because it actually schedules something rather than
  answering `ok`.
- **A `url_for` naming an endpoint that does not exist**, in a redirect after a
  successful sign-in — so the user was logged in and looking at an error page.
  The template scan could not see it; there is now a scan of the Python too.
- **`_first_fit` discarded the blockers on success**, so a levelling move said
  "shifted three days" without saying what was in the way.
- **A same-day activity derived a duration of zero** in the massing adapter,
  which made it a milestone and dropped it off the critical path.

### Notes

- On a multi-calendar schedule an activity can be **on the driving path and not
  critical**. A Mon–Sat activity handing to a Mon–Fri milestone across a
  Saturday boundary genuinely has a day of slack. Pinned by test, with the
  reasoning, so nobody "fixes" it.
- The derived-duration convention in the massing adoption kit is deliberately
  unchanged: `(finish - start).days`. Changing it would add a day to every
  date-derived duration in every existing project — a data migration, not a bug
  fix. See `massingplan/integrations/massing/README.md`.
- The engine was vendored into `ibuilder/massing` by hand and then reverted: the
  vendored copy is a derived artifact and the adapter changes when this repo
  changes, so both live here and adoption is one command on the consumer's own
  branch.

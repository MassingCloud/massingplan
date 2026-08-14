# massingplan

**A construction scheduling engine you can check by hand.**

Multi-calendar CPM with all four relationship types and all ten constraint
types, DCMA schedule-quality assessment, Primavera XER and MS Project
interchange that keeps the logic, Monte Carlo risk, resource levelling, and
baseline-to-baseline delay attribution that sums.

Standalone and self-hostable. Its engine is pure standard library, so it also
drops into other products' source trees unchanged.

```bash
pip install -e ".[dev]"
massingplan check          # boot and report the resolved config
pytest -q
```

## Why it exists

Most scheduling tools compute sophisticated analytics on top of a network that
cannot represent a real schedule, and nothing in the output says so.

The two systems this was extracted from are both examples. One has excellent
Earned Schedule, Monte Carlo and delay-claim analysis running on a
Finish-to-Start-only CPM core with no calendars, no lags and no constraints —
and a Primavera importer that drops the relationship table, so every imported
activity reports as critical with zero float. The other has a genuinely good CPM
engine that works in working-day offsets, which is correct only while every
activity shares one calendar, and which surfaces none of its own quality scoring
in its own interface.

So the starting position here is: **get the network right, prove it, and say so
when you cannot.**

## What it does

**Scheduling.** Forward and backward pass on an absolute ordinal time axis with
per-activity calendars. FS, SS, FF and SF with leads and lags, and an explicit
choice of whose calendar the lag is measured in. All ten Primavera constraint
types, split into soft (never override logic; an unmet one shows as negative
float) and mandatory (do override logic, in both passes). Data date, actual
dates, remaining duration as a first-class field, and the retained-logic versus
progress-override switch that decides what an out-of-sequence update means.

**Quality.** All fourteen DCMA checks. A check with no baseline reports
*skipped*, not *passed*, and is excluded from the score's denominator — the
score is the percentage of runnable checks, not a number inflated by the ones
that could not run. Check 12 actually runs the critical path test rather than
asserting it.

**Interchange.** Primavera XER and MS Project MSPDI, read and write, with no
third-party dependency. Including the tables that get dropped elsewhere:
`TASKPRED`, `SCHEDOPTIONS`, and real calendar exceptions — a two-week shutdown
that goes unparsed moves every downstream date two weeks early.

**Risk.** Monte Carlo with PERT or triangular distributions, criticality index,
duration sensitivity, and percentiles that round up, because a P80 of 271.2 days
is not met on day 271.

**Levelling.** Deterministic serial schedule generation over renewable
resources, with a `within float` horizon that will not move the project finish
and reports what it could not resolve rather than quietly extending the date.

**Delay analysis.** Baseline-to-baseline comparison with explicit identity
matching, driving-path deltas, and attribution whose contributions sum exactly
to the finish move. A residual is named `PATH_SWITCH` or `UNEXPLAINED`, never
dropped to make the arithmetic work.

**Line of balance.** Location-based scheduling with **crew continuity** — the
thing CPM structurally cannot express. The line shift takes its maximum over
*every* location, which is what stops a faster successor overtaking its
predecessor somewhere in the middle of the building while both ends still look
clear. It reports the **binding location**, where the buffer is fully consumed
and a slip is felt first, and the continuity cost in crew-days of float given
up to keep crews whole.

**Takt planning.** The same zones and the same take-off, planned as a train
instead: every wagon occupies one zone for exactly one takt, the crew sizes
move so the durations do not, and the duration is `(wagons + zones − 1) × takt`
— readable off the plan before any of the work is estimated. What it costs is
reported unrounded, per wagon: utilisation below 1.0 is capacity bought and not
worked, and rounding it is how a takt plan comes to look efficient while a
third of a trade stands about. A wagon that cannot meet the takt within the
crews it can field is refused, not squeezed.

**Last Planner.** Commitments, the constraint log, and PPC — plan reliability
rather than progress. Built around the fact that PPC is trivially gameable: the
denominator is frozen when the week is committed, partial completion is not
partial credit, an unassessed commitment makes the week *unmeasurable* rather
than perfect, a missed commitment without a reason is refused, and constrained
work cannot be committed at all. The trend is reported as a series, never as
one lifetime average — five good weeks and one collapse is a project with a
problem in week six, and 72% shows nothing.

## The application

The engine is the point, but it ships inside a working self-hostable app:
Flask and Jinja, a self-hosted SVG Gantt and line-of-balance chart with no CDN
and `default-src 'self'` intact, SQLAlchemy persistence with Alembic
migrations, organisations and four roles, API keys hashed at rest, an
append-only audit log, signed outbound webhooks, optional TOTP two-factor, and
optional **SSO** over OIDC authorization-code with PKCE.

`massingplan/core/` needs none of it. It is pure standard library, held that
way by an import-linter contract and a dependency-free CI job, so it can be
vendored into another product's source tree and imported with no packaging
change.

## Design commitments

- **Hand-checkable.** Every algorithm has a test network a human can verify with
  a pencil, and the expected values are written into the test file rather than
  generated by the code under test.
- **Deterministic.** Same input, same answer, across runs and hash seeds. Every
  sort has a total-order tiebreak.
- **Nothing defaulted silently.** Every coercion, drop or fallback emits an
  issue with a stable code and the action taken.
- **Negative float is an output.** It is never clamped — not total float, not
  free float.
- **Core never writes.** It computes and returns; the caller persists.

## Status

Alpha, built in phases through P13. See `SPEC.md` §11 for the plan and what
each phase has to prove before the next starts.

What is deliberately **not** here, so it is not discovered later: there is no
mobile or offline client for Last Planner — it is a planner's board, not a
foreman's phone — and no lookahead board, because storing rejected commitments
beside accepted ones would put them one boolean away from the PPC denominator.
`SECURITY.md` carries the same treatment for the security posture, including
the two things that have not been done: no penetration test, and no run against
a commercial identity provider.

## Documentation

- `SPEC.md` — the research, the time-axis decision, the precedence stack, the
  phase plan
- `ROADMAP.md` — what comes next, what it has to prove, and what is
  deliberately not coming
- `AGENTS.md` — architecture rules, golden rules, where things live
- `CONTRIBUTING.md` — what will get a pull request sent back

## Licence

MIT. Portions of the engine derive from `ibuilder/AIHackScheduler` (MIT); the
provenance is recorded in the header of each file that carries ported code.

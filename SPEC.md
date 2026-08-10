# massingplan — specification

> A standalone construction scheduling engine whose core is pure enough to be
> vendored into another product without change.

## 1. Why this exists

### 1.1 The problem in the field

A construction schedule is a production control system, not a date list. The
strongest operating model uses CPM for contractual logic, location-based methods
for flow and space, Last Planner for reliable crew commitments, rolling-wave
detail for uncertainty, and optimisation only after the logic is sound. Most
software gets one of those layers right and fakes the rest.

The failure mode that matters is narrower and more specific: **tools compute
sophisticated analytics on top of a network that cannot represent a real
schedule**, and nothing in the output says so.

### 1.2 The two source systems

**`ibuilder/massing`** has an exceptional analytics layer — EVM with Earned
Schedule, Monte Carlo, extension-of-time claims per AACE 29R-03, takt, a live
Last Planner board, resource levelling within float, schedule optioneering with
a Pareto frontier. It computes all of it from a 105-line CPM core that is
Finish-to-Start only, has no lags, no calendars and no constraints, and never
writes computed dates back to the activities. Its Primavera importer drops the
`TASKPRED` table entirely, so importing a real P6 file produces a network with
no logic at all — and a network with no logic reports every activity as critical
with zero float. The analytics are correct; their input is not.

**`ibuilder/AIHackScheduler`** contains, buried inside a generic Flask CRUD
application, roughly 1,600 lines of genuinely good scheduling code: a CPM engine
with all four relationship types, working calendars, all fourteen DCMA checks
with honest skip semantics, Monte Carlo risk, and hand-written Primavera XER and
MS Project MSPDI readers and writers with the field-mapping traps documented
next to the code that avoids them. None of it is reachable from anywhere else,
none of it is surfaced in that product's own UI, and it works in working-day
offsets, which is correct only while every activity shares one calendar.

### 1.3 What this repo is

The engine both of them should have been built on, extracted so it can be used
on its own and consumed by both.

## 2. Non-goals

- **Not a Primavera replacement.** Interchange fidelity is a goal; feature parity
  is not.
- **Not an AI scheduler.** Optimisation hooks exist (`priority_key`,
  `objective()`); no metaheuristic ships in v1. A non-deterministic optimiser
  cannot be hand-checked, and hand-checkability is the property this package
  trades everything else for.
- **Not a field app.** Last Planner production control is phase P11+, after the
  engine is sound.
- **No binary `.mpp` writing.** The format is proprietary and undocumented;
  interchange is via MSPDI XML.

## 3. Architecture

```
                        ┌──────────────────────────────┐
                        │  blueprints/  (Flask, Jinja) │
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────┴───────────────┐
                        │  api/   framework-agnostic   │ ← massing mounts this
                        │  plain functions over dicts  │   under FastAPI
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────┴───────────────┐
                        │  services/   ORM ↔ core      │
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────┴───────────────┐
                        │  models/   SQLAlchemy 2.0    │
                        └──────────────┬───────────────┘
                                       │
        ════════════════════════════════════════════════════════
                        ┌──────────────┴───────────────┐
                        │  core/   PURE STDLIB         │ ← vendored verbatim
                        │  zero third-party imports    │   into ibuilder/massing
                        └──────────────────────────────┘
        ─ ─ ─ ─ ─ ─ ─ ─ everything below is OPTIONAL ─ ─ ─ ─ ─ ─
          services/entitlement/massing_cloud   services/identity/oidc
          services/storage/s3
```

### 3.1 The contracts, and why they are build failures

`pyproject.toml` carries five import-linter contracts. Two of them are the
design:

- **`core` is pure.** It may not import the web app, the ORM, pydantic, or any
  adapter. This is what makes the vendoring work: `core/` is copied into
  `massing`'s `services/api/src/`, which is already on `PYTHONPATH`, and it
  imports there with no packaging change. The contract exists because the edit
  that breaks it ("just import the model here") looks harmless in this repo and
  fails in the other one, weeks later.
- **`api` is framework-agnostic.** Flask lives in `blueprints/`. An `api` module
  that imports `flask` silently makes the second consumer impossible, and
  nothing would notice until someone tried.

Plus a `no-adapters` CI job that deletes every optional adapter and re-runs the
suite, and an `offline` job that patches sockets to raise, walk-imports every
module and boots the app. A decoupling nobody tests is a decoupling that has
already been broken.

### 3.2 Wire conventions shared with massing.cloud

Honoured unconditionally, with no dependency on massing:

| Convention | massingplan |
|---|---|
| REST namespace | `/api/massingplan/v1/…`, same noun style, same error table |
| Auth | `Authorization: Bearer <key>` / `X-Api-Key`, org-scoped, hashed at rest, prefix `mpln_` |
| Webhook signing | hex HMAC-SHA256 in `X-Massing-Signature` |
| Entitlement object | exact field names and semantics, `UNLIMITED = -1` |
| Secrets | env only, never the database |

## 4. The time axis — the decision everything else rests on

**The CPM pass operates on an absolute integer `Instant = date.toordinal()`,
interpreted as a boundary at 00:00 of that ordinal day. Activity spans are
half-open: `[start, finish)`.**

An activity that works D₁…Dₙ has `start = ordinal(D₁)` and
`finish = ordinal(Dₙ) + 1`. The displayed finish is `finish - 1`, the last day
worked, converted at exactly one site.

### 4.1 Why not working-day offsets

Both source repos use integer working-day offsets from day zero and map them to
dates afterwards. That is correct only while every activity shares one calendar.

- Activity **A**, Mon–Fri, with a holiday on Mon 8 June. Offset 5 from 1 June is
  **Mon 15 June**.
- Activity **B**, Mon–Sat, no holidays. Offset 5 from 1 June is **Sat 6 June**.

The forward pass computes `earliest = max(earliest, predecessor_ef + lag)`.
`max(5, 5)` is `5`. It has just compared Mon 15 June with Sat 6 June and
returned "5". The successor starts nine days early, and nothing raises.

Offsets are the bug, not the representation.

### 4.2 Why not a global serial working-day index

A global index must be defined against some calendar. An activity on a five-day
calendar then occupies a non-contiguous set of global indices, so
`finish = start + duration` stops holding, and every operation needs a
per-activity translation whose codomain is the ordinal-day axis. Use the ordinal
axis directly.

### 4.3 Why half-open

Inclusive finish makes Finish-to-Start asymmetric: the successor starts on
`next_working_day_after(predecessor_finish)`, and "after" must be evaluated in
someone's calendar. Half-open makes all four relation types pure comparisons on
one axis, with no `±1` anywhere:

| Type | Forward-pass bound on the successor |
|---|---|
| FS | `succ.start ≥ lag_advance(pred.finish, lag)` |
| SS | `succ.start ≥ lag_advance(pred.start, lag)` |
| FF | `succ.finish ≥ lag_advance(pred.finish, lag)` |
| SF | `succ.finish ≥ lag_advance(pred.start, lag)` |

The inclusive-finish reading survives as a display rule, in one function.

### 4.4 Milestones

A zero-duration activity has `start == finish`. Display is asymmetric: a **start
milestone** displays at `start`; a **finish milestone** displays at `finish - 1`
snapped back to the previous working day. Without the asymmetry, "Substantial
Completion" — a finish milestone tied FS to the last work — prints one day after
the work it marks, and every contractual milestone in the export is a day late.

### 4.5 The lag calendar

Lag is measured in working days, but whose? `LagCalendar` is an explicit option,
default `PREDECESSOR` (P6's default), read from the file's `SCHEDOPTIONS` on
import and recorded on the result.

Predecessor on Mon–Fri finishes Fri 5 June. FS with a 5-day lag. Successor on
Mon–Sat:

- Lag on the **predecessor's** calendar → successor starts **Sat 13 June**.
- Lag on the **successor's** calendar → successor starts **Fri 12 June**.

One day on a clean example, more once holidays differ. Choosing silently makes
an imported P6 schedule disagree with P6, and both answers look plausible, so
nobody catches it.

## 5. The precedence stack

Constraints, the data date, ALAP and the resource leveller all want to move the
same activity. The order is fixed, stated in `core/cpm.py`'s docstring, and
asserted by a test that builds an activity subject to all eight.

1. **Actual dates** — recorded history. Nothing moves an actual.
2. **Data date floor** — no remaining work is scheduled before the data date.
3. **Mandatory constraints** — override logic in both passes; still floored
   by (2), and the violation is recorded with `overridden_by="data_date"`.
4. **Soft constraints** — never override logic. An unmet soft constraint is
   negative float plus a `ConstraintViolation`.
5. **Network logic + lag.**
6. **Calendar snapping** — always last inside the pass.
7. **ALAP post-pass** — shift right by **free** float, reverse topological
   order. Shifting by total float would move the project finish, which is
   precisely what ALAP is defined never to do.
8. **Resource levelling** — a separate module, strictly after. It may only move
   activities later, never earlier, and never violates 1–6.

## 6. Data model

`Project` · `Calendar` · `CalendarException` · `WBSNode` · `Zone` · `Activity` ·
`Relationship` · `Resource` · `Assignment` · `Baseline` · `Scenario` ·
`ImportJob` · `Issue` · `AuditLog`

Fixes carried deliberately from AIHackScheduler's model, each of which was a
real defect there:

- `status` and `dependency_type` are enums, not bare strings.
- `remaining_duration` is a column, not derived from percent complete.
- Username and email are unique **per tenant**, not globally.
- No column named `*_encrypted` that nothing encrypts.
- Every index is declared in `__table_args__`, so `create_all()` and Alembic
  produce the same schema on SQLite and Postgres alike.

## 7. Determinism

Same input, same answer — across runs, across `PYTHONHASHSEED`, across
platforms.

- Every sort has a total-order tiebreak ending in a stable id. Without it, dict
  and set iteration order leaks into resource levelling and the same input
  produces different dates between runs. An optimiser whose answer changes
  between runs cannot be reviewed, approved, or defended in a claim.
- Monte Carlo is seeded by default.
- The topological sort is seeded in declaration order, so a logic-free schedule
  round-trips in input order.
- A `determinism` CI job runs the engine suite twice under different hash seeds
  and diffs the outputs.

## 8. The kernel

`core/timeaxis.py`. **The calendar adjoint invariant:**

```
C.sub_working_days(C.add_working_days(i, n), n) == i
C.add_working_days(C.sub_working_days(i, n), n) == i
C.count_working_days(i, C.add_working_days(i, n)) == n
is_working(C.finish_from_start(i, d) - 1)          for all d ≥ 1
C.finish_from_start(i, 0) == i
```

`add` and `sub` are exact inverses on the working-day lattice, and `count` is
their inverse.

Every other module assumes this silently. The forward pass derives finishes with
`add`; the backward pass derives late starts with `sub`; float is `count`
between them; levelling steps spans with `add`; risk calls all three two
thousand times; the XER writer converts back through `count`.

If `add` and `sub` are off by one on exactly the instants that straddle a
holiday, the error appears only on activities crossing a shutdown — which are
disproportionately the ones on the critical path, because a shutdown is where
slack gets consumed. The dates stay plausible. Nothing raises. The critical path
is wrong by a day, and stays wrong through the export, the DCMA report, the risk
P80 and the delay claim.

So: 100% branch coverage, its own CI job, and an exhaustive stdlib sweep — seven
weekday masks × four holiday shapes × `n ∈ 0…400` × start instants across a
two-year window, roughly 10⁶ assertions in a few seconds, written as nested
loops a human can read rather than a property-testing DSL.

## 9. Honesty rules

Adopted from the better half of both source repos, where each was learned the
hard way.

- **`passed=None` means skipped, not passed.** A DCMA check with no baseline
  reports "skipped: no baseline supplied" and is excluded from the score's
  denominator. The score is the percentage of *runnable* checks.
- **`None` with a reason, never zero.** Zero float and unknown float are
  different statements. Baseline Execution Index returns `None` when nothing was
  due, rather than calling an empty ratio 1.0.
- **Nothing is defaulted silently.** Every coercion, drop or fallback emits an
  `Issue` carrying a stable greppable code (`XER.TASKPRED.UNKNOWN_TYPE`), the
  action taken (`coerced to FS`), and the raw value. Prose warnings in a list of
  strings cannot be filtered, counted or asserted on, so the tests on them get
  deleted and then nobody checks them at all.
- **Errors disclose nothing.** Unauthenticated health endpoints log the real
  cause and return a message that gives away no internals.
- **404, not 403, on a cross-tenant read.** "This exists but is not yours" tells
  one contractor that another contractor's project id is real.

## 10. Interchange

**Primavera XER** — read and write, pure Python, no dependency. The four traps
documented in the source and preserved here:

1. `PR_FS` / `PR_SS` prefixes are mapped by hand, not by string-slicing.
2. Hours per day comes from `CALENDAR.day_hr_cnt`, never a hardcoded 8.
3. Zero-duration milestones are not clamped to 1 day.
4. Costs come from `TASKRSRC` and notes from `TASKMEMO`, not from `TASK`.

Extended here with the tables the previous implementations dropped:
`SCHEDOPTIONS` (retained logic, lag calendar), **`TASKPRED`** (the table
massing's importer omits entirely), `RSRC`, `ACTVCODE`, `UDFVALUE`, `NONWORK`,
and **real calendar exceptions** — the previous reader returned an empty
exception list, so a two-week Christmas shutdown moved every downstream date two
weeks early.

**MS Project MSPDI** — read and write. Three traps preserved: ISO-8601 durations
(`PT40H0M0S`), link `Type` integers are `0=FF, 1=FS, 2=SF, 3=SS` and are
deliberately not alphabetical, and `UID` is not `ID`.

Round-trip tests assert **the computed schedule does not move**, not that the
bytes match.

## 11. Phases

Each phase meets its acceptance criteria, with tests, before the next begins.

| Phase | Delivers | Acceptance |
|---|---|---|
| **P0** | Repo skeleton, config, adapter seams, CI | `offline`, `no-adapters`, `imports` green on an app that serves `/healthz` |
| **P1** | `timeaxis` · `units` · `issues` · `graph` | `timeaxis-kernel` at 100% branch coverage; the sweep passes |
| **P2** | `constraints` · multi-calendar `cpm` | 30 hand-checked constraint cases; the cross-calendar chain by hand; driving path stable under input shuffling |
| **P3** | `progress_logic` · `model` · `schedule` | Retained logic and progress override give two different hand-computed finishes; `to_rows()` key set frozen |
| **P4** | `health` · `progress` · `risk` | 14 DCMA checks with skip semantics; seeded Monte Carlo byte-identical across runs |
| **P5** | `xer` · `mspdi` full fidelity | Read→schedule→write→read preserves computed dates; a shutdown moves the finish correctly; an unknown `pred_type` yields an `Issue` and still imports |
| **P6** | API layer, models, Flask app | Every route walked; every `url_for` endpoint registered |
| **P7** | Frontend bundle | CSP unchanged; interactive on a 2,000-activity network |
| **P8** | `resources` · `levelling` | Textbook one-crew case by hand; identical output under varied `PYTHONHASHSEED`; `WITHIN_FLOAT` never moves the finish |
| **P9** | `compare` | Attribution sums exactly to the finish move |
| **P10** | Vendor into massing | A real XER imports with logic intact and a non-flat critical path |
| **P11** | `locations` — location-based scheduling | Crew continuity holds; the line shift takes its maximum over every location; the emitted network schedules to the same dates through the ordinary engine |
| **P12+** | Takt planning; Last Planner production control | — |

## 12. Vendoring into massing

`massingplan/core/` is copied verbatim to
`ibuilder/massing:services/api/src/massingplan/`, which is already on
`PYTHONPATH` per that repo's `services/api/Dockerfile`. This mirrors the Python
side of the pattern that repo already uses for `massingifc` and `massingpdf` in
`apps/web/src/vendor/`.

- `VENDOR.md` pins the upstream commit SHA, states "Local deviations — NONE",
  and gives the exact re-sync commands.
- massingplan's own core tests are copied and run by massing's pytest. A
  vendored library nobody exercises is a fork you have not noticed yet.
- A weekly divergence workflow asks whether the pinned SHA has gone stale, and
  **hard-fails when the query itself breaks** rather than reporting all-clear.

Publishing `massingplan-core` to PyPI and hash-pinning it into massing's
`requirements.lock` is the follow-on, not the blocker.

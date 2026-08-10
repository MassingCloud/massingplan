# Massing Plan — Agent Operating Guide

## What this is
A standalone, self-hostable construction scheduling engine: multi-calendar CPM,
the ten constraint types, data-date and progressed logic, DCMA schedule quality,
Primavera XER and MS Project MSPDI interchange, resource levelling, Monte Carlo
risk, and baseline-to-baseline delay attribution. **Read `SPEC.md` first** — it
carries the research, the time-axis decision, the precedence stack, and the
phase plan with acceptance criteria.

## Architecture (do not drift)
- Python 3.11+ · Flask 3 · SQLAlchemy 2.0 · Alembic · Jinja server-rendered UI.
  Postgres in production, SQLite in dev and tests.
- **`massingplan/core/` is pure stdlib.** No Flask, no SQLAlchemy, no pydantic,
  no third-party anything. It is copied verbatim into `ibuilder/massing` at
  `services/api/src/massingplan/`, where none of those are guaranteed to exist.
  This is not a style preference; it is the reason the vendoring works.
- **Standalone is the product, not a mode.** Zero runtime dependency on
  massing.cloud. Every external touchpoint is one of three adapter ABCs
  (`services/entitlement`, `services/identity`, `services/storage`), default
  implementation local, optional implementation lazily imported.
- The layers run one way:
  `core/` → `models/` → `services/` → `api/` → `blueprints/`.
  **`api/` never imports Flask.** It is plain functions over primitives, so
  massing can mount the same surface under FastAPI with a fifteen-line adapter.

## Golden rules
1. **Time is an absolute ordinal integer** (`Instant = date.toordinal()`), and
   activity spans are **half-open**: `[start, finish)`. Working-day offsets are
   the bug this package exists to fix — they are only commensurable within one
   calendar, and the forward pass compares them across calendars without
   noticing. See `SPEC.md` §4.
2. **Display finish is `finish - 1`** — the last day worked. The conversion
   happens at exactly one site, `core/schedule.py::_present`. Nowhere else.
3. **Working-day arithmetic goes through `core/timeaxis.py`.** It is the kernel:
   100% branch coverage, its own CI job, and the adjoint invariant
   (`sub(add(i, n), n) == i`) that every other module assumes silently.
4. **`core/units.py` owns the only `ceil` in the package.** Hours-to-days
   conversion happens there and only there, with `ROUNDING_EPSILON`.
5. **Negative float is an output, never an error, and is never clamped.** Not
   total float, not free float. A clamp hides the exact signal the planner set
   the constraint to see.
6. **Nothing is defaulted silently.** Every coercion, drop or fallback emits an
   `Issue` with a stable greppable code and the action taken. A silent default
   produces a plausible schedule that is wrong, with nothing in the output to
   look at.
7. **Core never writes.** It computes and returns; the caller persists.
   `apply_to()` returns a new object rather than mutating its argument.
8. **Deterministic or it does not ship.** Same input, same answer, across runs
   and across `PYTHONHASHSEED`. Every sort has a total-order tiebreak. An
   optimiser whose answer changes between runs cannot be reviewed or defended.
9. **Actuals are history.** Nothing moves a recorded actual date — not a
   constraint, not the data date, not the leveller. An actual outside the
   expected range is reported, not clamped.
10. **No adapter may leak into the core.** If you find yourself importing
    `massing_cloud`, `oidc` or `s3` from a blueprint or a model, the design is
    wrong — add a method to the ABC instead.

## The precedence stack
When several things want to move the same activity, this is the order. It is
stated in `core/cpm.py`'s docstring and asserted by a test.

1. Actual dates — recorded history
2. Data date floor — you cannot work yesterday
3. Mandatory constraints — override logic in both passes
4. Soft constraints — never override logic; unmet ones produce negative float
5. Network logic + lag
6. Calendar snapping — always last inside the pass
7. ALAP post-pass — shift by **free** float, reverse topological order
8. Resource levelling — separate module, strictly after, may only move later

## Where things live (`massingplan/`)
| Path | Contents |
|---|---|
| `core/timeaxis.py` | The kernel. `WorkCalendar`, lattice index, working-day arithmetic |
| `core/units.py` | The single rounding site; the `date`-not-`datetime` guard |
| `core/issues.py` | `Issue`, `IssueLog`, severity, stable codes |
| `core/graph.py` | Topological order, cycle naming, driving-chain walk |
| `core/constraints.py` | The ten constraint types as a semantics table |
| `core/progress_logic.py` | Data date, actuals, retained logic vs progress override |
| `core/cpm.py` | Multi-calendar forward/backward pass |
| `core/model.py` | The format-neutral hub model (`ExchangeSchedule`) |
| `core/schedule.py` | `schedule()` → `ScheduleOutcome`; the one conversion site |
| `core/health.py` | The 14 DCMA checks |
| `core/progress.py` | BEI, finish variance, slippage |
| `core/risk.py` | Monte Carlo, criticality index, duration sensitivity |
| `core/resources.py` | Demand profiles, over-allocation |
| `core/levelling.py` | Serial SGS resource levelling |
| `core/compare.py` | Baseline diff and delay attribution |
| `core/xer.py` · `core/mspdi.py` | Primavera and MS Project read/write |
| `api/` | Framework-agnostic functions over primitives |
| `services/entitlement/` | `base` · `standalone` (default) · `massing_cloud` |
| `services/identity/` | `base` · `local` (default) · `oidc` |
| `services/storage/` | `base` · `local` (default) · `s3` |

## Testability contract
The suite runs **offline and deterministically** on SQLite with zero
infrastructure.
- Every engine algorithm has a network a human can check with a pencil. Expected
  values are hand-computed and written into the test file, never generated by
  the code under test.
- `tests/conftest.py` blocks outbound sockets. An accidental network call is a
  loud failure, not a slow test.
- Settings are constructed explicitly; a developer's `.env` cannot change a
  result.

## One deliberate deviation
massingbill's house rule is zero JavaScript. A schedule tool needs a Gantt, so
`massingplan/static/js/gantt.js` exists. **No CDN, no external font, no
third-party chart library** — the strict `default-src 'self'` CSP in
`security.py` stays intact, and a test asserts no `<script src="http...">`
appears in any template.

**There is no build step and no `frontend/` bundle.** This document claimed one
for a while; it did not exist, and neither did anything that checked the
JavaScript. The Gantt shipped for weeks drawing no dependency arrows at all,
because `to_rows()` never carried `predecessors` and the renderer read
`(row.predecessors || [])` — an absent contract became an empty list and the
feature was silently missing.

So: the file served to the browser is the file in the repo, and it is
type-checked in place.

```bash
npx tsc -p jsconfig.json      # `checkJs`, `noEmit`, strict
```

TypeScript is a dev-only checker, not a language this project is written in. The
`Row` typedef at the top of `gantt.js` is the renderer's half of the contract in
`api.schedules.chart_rows()`, and `tests/test_frontend.py` asserts the two
agree — because the half nobody wrote down is the half that went missing.

## Workflow
Work phase by phase per `SPEC.md` §11. Meet a phase's acceptance criteria, with
tests, before starting the next. Run the commands below before every commit.

## Commands
```bash
pip install -e ".[dev]"           # install
pytest -q                         # test
pytest -m kernel --cov=massingplan/core/timeaxis --cov-branch --cov-fail-under=100
ruff check . && ruff format .     # lint + format
mypy massingplan                  # type check
lint-imports                      # decoupling contracts
massingplan check                 # boot and report resolved config
```

## CI expectations (all blocking)
`lint` (ruff check + ruff format --check + mypy) · `test` on 3.11/3.12/3.13 ·
`timeaxis-kernel` (100% branch coverage on the kernel) · `offline` (imports and
serves with the network dropped) · `no-adapters` (deletes every adapter, re-runs
the suite) · `imports` (import-linter) · `determinism` (runs the suite twice
under different `PYTHONHASHSEED` and diffs the engine outputs).

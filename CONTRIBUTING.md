# Contributing

Read `AGENTS.md` first, then `SPEC.md`. They carry the architecture and the
reasoning; this file is the short list of things that will get a pull request
sent back.

## Six ways to get a change rejected

1. **An import into `massingplan/core/` from outside the standard library.**
   `core` is copied verbatim into other products' source trees. One
   `import pydantic` and the vendoring stops working — and it fails in the
   *other* repo, weeks later. `lint-imports` catches it; do not add an
   exemption.

2. **A number where `None` belongs.** Zero float and "this activity is finished"
   are different statements. A DCMA check with no data reports *skipped*, not
   *passed*, and leaves the score's denominator. Baseline Execution Index
   returns `None` when nothing was due, not 1.0.

3. **Clamping negative float**, in either direction, in the engine or in the
   presenter. It is the output. It is the reason the planner set the constraint.

4. **A silent default.** Every coercion, drop and fallback emits an `Issue` with
   a stable code and the action taken. A silent default produces a plausible
   schedule that is wrong, with nothing in the output to look at.

5. **A test whose expected value came from running the code.** Engine
   expectations are worked out by hand and written into the test file. A test
   that takes its answer from the implementation only proves the implementation
   agrees with itself.

6. **Non-determinism.** Every sort ends in a total-order tiebreak. Same input,
   same answer, across runs and hash seeds — an optimiser whose answer changes
   between runs cannot be reviewed, approved, or defended in a claim. The
   `determinism` job runs the suite twice under different `PYTHONHASHSEED`.

## Comments

Explain *why*, and name the failure the code prevents. `# snap the boundary` is
noise. `# a finish boundary on Saturday belongs to Friday's work -- snapping it
forward puts Substantial Completion a day after the work it marks` is the reason
somebody will not simplify it back.

## Before every commit

```bash
pytest -q
```

```bash
ruff check . && ruff format . && mypy massingplan && lint-imports
```

## The kernel

`massingplan/core/timeaxis.py` has 100% branch coverage and its own CI job. If
you optimise it, you have to prove the branch you removed was unreachable.
Everything else in the engine assumes its adjoint invariant without checking,
and a violation is invisible: the dates stay plausible and nothing raises.

## Database changes

A model change without a migration fails the `migrations` job on `alembic
check`. Generate one, rename it to the next `NNNN_slug.py`, and confirm
up-down-up works — on Postgres too, which the `postgres` job does for you.

## Commits

Conventional prefixes preferred, not enforced. Write the body as prose that says
what was wrong and why this is better — the git log is the only place that
reasoning survives contact with the next reader. End with:

```
Co-Authored-By: Your Name <you@example.com>
```

# Adopting massingplan into `ibuilder/massing`

Everything needed to replace massing's CPM core with this engine, kept **here**
rather than there.

## Why the adapter lives upstream

The vendored engine is a derived artifact: `scripts/vendor_to_massing.py`
produces it from a pinned commit. The adapter is the same kind of thing. It
encodes knowledge about both sides, but it *changes when this repo changes* --
a new relationship type, a new field on `Task`, a renamed row key -- so it
belongs with the thing that moves.

Keeping it here has one practical consequence worth stating plainly: **massing's
adoption is one command, run by whoever owns that repo, on their own branch, at
a moment they choose.** Hand-landing it there instead means competing for the
same HEAD and index as every other branch in an active repo, and it makes an
engine release look like a change to the consumer.

## What is in here

| File | Becomes | What it does |
|---|---|---|
| `schedule_engine.py` | `services/api/src/aec_api/schedule_engine.py` | Maps `mod_schedule_activity` records to `Task`/`Link`/`WorkCalendar` and back to the legacy row shape. The only place the two models meet. |
| `schedule_cpm.py` | `services/api/src/aec_api/schedule_cpm.py` | Replaces the 105-line engine with a shim. `compute()` keeps the exact dict every caller reads. |
| `schedule_import.py` | `services/api/src/aec_api/schedule_import.py` | Full-fidelity XER/MSPDI import, including the `TASKPRED` table the current importer omits. |
| `research-importer.patch` | `routers/research.py` | Routes the upload endpoint through the above, and returns an import report. |
| `test_adoption.py` | `services/api/tests/test_schedule_engine_vendored.py` | Proves the legacy contract still holds and that a real XER imports with its logic. |
| `vendor-massingplan-drift.yml` | `.github/workflows/` | Weekly staleness check on the pin, and runs upstream's tests. |

## Why it is worth adopting

`aec_api/schedule_cpm.py` is Finish-to-Start only, with no lags, no calendars
and no constraints, and it never writes computed dates back. Separately,
`routers/research.py` never reads `TASKPRED`.

That second one is the sharp end. **A network with no relationships has no
critical path**: every imported activity comes back with zero float and reads as
critical, and the import reports success. EVM, Monte Carlo, the AACE 29R-03
extension-of-time methods and resource levelling within float are all correct
implementations reading an input that is wrong.

## Adopting

```bash
# from a massingplan checkout, with massing on a branch of its own choosing
python scripts/vendor_to_massing.py --target <massing>/services/api/src/massingplan
cd <massing>/services/api && PYTHONPATH=src pytest tests/vendor_massingplan tests/test_schedule_engine_vendored.py -q
```

Then apply `research-importer.patch` and run massing's own `test_cpm.py` and
`test_eot.py` -- both pass against the new engine unchanged.

## Two behaviour changes to agree before merging

**A circular network no longer returns dates.** The old code broke the loop in
dictionary order and handed back a full set of numbers, flagged only by
`has_cycle` -- and EVM consumed them. Callers still get every activity; every
computed field is now `None` and the loop is named in `cycle`.

**A same-day activity is one day of work, not zero.** `max(0, (finish -
start).days)` made it a milestone and dropped it off the critical path.

## One decision that is massing's, not this repo's

The *derived-duration convention* is deliberately unchanged: `(finish -
start).days`, so 1 to 11 January is ten days. That reads the stored finish as an
exclusive boundary, and is arguably off by one against P6's inclusive
`target_end_date`.

Changing it would add a day to every date-derived duration in every existing
project. That is a data migration, not a bug fix, and not one to make by side
effect. See `schedule_engine._duration_days`.

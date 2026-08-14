# Roadmap

`SPEC.md` §11 carried the build plan, P0 through P13. Every phase in it has
shipped and met its acceptance criteria, so that table is now a record rather
than a plan. This file is the forward half: what comes next, why, and what each
item has to prove.

It also states what is **deliberately not coming**, in the same place. A
non-goal discovered eighteen months in reads as an omission; a non-goal written
down reads as a decision.

---

## Where this stands

| | |
|---|---|
| Engine | Multi-calendar CPM, all four relation types, all ten constraint types, data date and progressed logic, DCMA 14-point, Monte Carlo, resource levelling, baseline comparison with delay attribution, line-of-balance, takt, Last Planner |
| Interchange | Primavera XER read/write, Primavera P6 XML (PMXML) read/write including baselines, MS Project MSPDI read/write |
| Forensics | Baseline comparison; contemporaneous windows analysis (AACE 29R-03 MIP 3.3); impacted as-planned and collapsed as-built (MIP 3.6 and 3.9) with concurrency reported |
| Performance | BEI and variance, plus Earned Schedule reported beside the classic index it replaces |
| Planning | Weather allowance in the calendar rather than in durations; schedule compression as priced options; multi-project portfolios with links across the boundary |
| Core | ~11,600 lines, pure standard library, vendored into `ibuilder/massing` |
| Gates | 19 CI jobs (17 definitions, `test` matrixed over 3.11/3.12/3.13); ~1,250 tests; 100% branch coverage on the calendar kernel |

The engine has also been probed with generated inputs — not only the tests
somebody thought to write. Random networks through XER (400 schedules), random
re-baselines through the attribution invariant (800), progressed schedules
against the precedence stack (700), the constraint table (600), line-of-balance
flows (200), takt trains (250), P6 XML round trips (300) and the compression,
portfolio and modelled-delay invariants (400). Those runs found eight defects,
all fixed;
where they found nothing, the existing tests were then deliberately broken to
confirm the coverage was real rather than absent.

---

## How something gets onto this list

Three questions, and an item needs all three:

1. **Is it a decision the tool currently makes silently?** The highest-value
   work in this codebase has consistently been removing a plausible-looking
   wrong answer, not adding a feature. A defaulted field beats a missing one
   for user-visible damage every time.
2. **Can it be checked by hand?** If a planner cannot verify the output against
   their own drawing, it does not ship — that rule produced the `(W + Z - 1)`
   takt formula and the attribution sum, and it is what makes the numbers
   arguable in front of a tribunal.
3. **Does it belong in `core/`?** `core` is copied verbatim into another
   codebase and imports nothing but the standard library. Anything needing a
   dependency belongs in `services/`, and saying so early is cheaper than
   discovering it at the vendoring gate.

---

## Shipped since this file was written

Every item below has landed. They are kept rather than deleted so the
acceptance criterion each was held to stays readable beside the result.

### R1 — Primavera P6 XML (PMXML) read and write — **shipped**

**Why.** XER is the routine transfer format and **it does not carry
baselines**; P6 XML does, along with the global data a restricted XER omits.
This tool's headline features include baseline comparison and windows analysis,
both of which need a *series* of dated schedules — and the format most likely to
be handed over for a claim is the one that can carry them. Reading XER only
means asking a client for eleven separate files and hoping the data dates
survived.

**Acceptance, met.** `read_p6xml_all` returns every project in the document,
so a file carrying baselines yields the series a windows analysis needs;
read → schedule → write → read preserves the computed dates; every default
taken emits an `Issue` naming the field. The XER round-trip probe ran against
P6 XML unchanged — 300 random schedules, zero drift, **including finish
milestones**, which MSPDI structurally cannot round-trip because it has one
boolean for both milestone kinds and this format has two enum values.
`core/p6xml.py`, 22 tests, four sabotages.

### R2 — Earned Schedule alongside the existing BEI — **shipped**

**Why.** `progress.py` reports BEI and variance, which say whether activities
finished when they should have. Earned Schedule says *how far along in time* the
project is, and unlike SPI(cost) it does not converge to 1.0 as a late project
finishes — which is precisely when a schedule metric needs to keep telling the
truth. It is schedule-domain arithmetic on data already in the model.

**Acceptance, met.** ES, SV(t) and SPI(t) from the baseline and the data date,
hand-checked; on the test project SPI(t) reads 0.5 at 100% complete where the
classic index reads 1.0, and both are returned side by side; `None` rather than
a number when no time has elapsed. `core/earned.py`, thirteen tests, five
sabotages. Kept in the list rather than deleted so the acceptance criterion it
was held to stays readable.

### R3 — Weather and calendar risk — **shipped**

**Why.** A shutdown is modelled (calendar exceptions round-trip through both
formats), but *expected* weather loss is not. On a UK or a monsoon-belt
programme it is the largest single systematic difference between a plan and an
outcome, and it is currently absorbed into activity durations where nobody can
see it or argue about it.

**Acceptance, met.** `core/weather.py` puts the allowance in the calendar as
non-working days, spread evenly through each month; a day already lost to a
shutdown is not lost twice; `without_allowance` removes only the weather days
and reproduces the original schedule exactly. Thirteen tests, three sabotages.

### R4 — Modelled delay methods (AACE MIP 3.6 and 3.9) — **shipped**

**Why.** `windows.py` is observational: it reads what the updates say and
changes nothing. The modelled methods — impacted as-planned (additive) and
collapsed as-built (subtractive) — insert or remove delay events to answer
"what would have happened but for this". Both are standard, both are asked for,
and both are a **different module** on purpose: mixing an observational method
with a modelled one in the same call is how an analysis acquires a conclusion
its inputs do not support.

**Acceptance, met.** `core/modelled.py`. Each method names its MIP; events are
listed with what each did alone and what they did together, and the difference
is reported as `concurrency_days` -- the most argued number in delay disputes.
A modelled result cannot come from `windows`: different types, different
modules, neither importing the other. Sixteen tests, three sabotages.

### R5 — Schedule compression — **shipped**

**Why.** Levelling answers "when can this be built with the crews I have".
Nothing answers "what would it take to finish three weeks earlier", which is
the question asked whenever a programme is late. The machinery is largely
present: `levelling.objective()` and `priority_key` are already the hooks.

**Acceptance, met.** `core/compression.py` returns options with a consequence
and applies nothing; crashing carries a cost, fast-tracking carries a stated
risk and no invented score; a day that buys nothing is not sold; the plan is
identical under three hash seeds. Sixteen tests, three sabotages.

### R6 — Multi-project and inter-project logic — **shipped**

**Why.** Every construction programme above a certain size is several
schedules with links between them, and a shared resource pool across them.
Today a project is scheduled alone.

**Acceptance, met, with one correction.** `core/portfolio.py`. External links
resolve across projects and the driving path crosses the boundary. The promise
that an unlinked project is *entirely* unchanged turned out to be too broad:
its dates are identical, but its float is measured against the programme --
which is the right answer, since a package finishing early genuinely has slack
against the completion it feeds. `standalone_rows_for` gives the package's own
float back. Nineteen tests, two sabotages.
---

## Next

Nothing is queued. The six items this file was written to describe have all
shipped, and the honest position is that the next one should come from a user
rather than from this document — the roadmap has run ahead of the evidence,
and inventing R7 here would be picking a feature because the section has a
heading.

What is worth doing before any of that is in the list below, which is where
the remaining known gaps actually live.

---

## Deliberately not on this list

These are stated in the modules themselves and repeated here so the roadmap is
the whole picture rather than the flattering half.

- **A metaheuristic optimiser for levelling.** `priority_key` and `objective()`
  are the hooks a simulated-annealing or genetic search would use. A
  non-deterministic optimiser cannot be hand-checked, and that is the rule the
  whole engine is built on.
- **Reverse flow in line-of-balance.** Strip-out often runs top-down. It is
  real and it is not modelled; every task runs the locations in the same order.
- **Mixed calendars within one line-of-balance flow**, and **multiple crews per
  task**. Both change the slope and the continuity rule, and guessing produces a
  plausible wrong answer. They raise an issue rather than being averaged.
- **Varying takt by zone.** A bigger floor plate getting two takts is real
  practice. It changes the duration formula, so it is absent rather than
  approximated.
- **A mobile or offline Last Planner client.** It is a planner's board, not a
  foreman's phone.
- **A lookahead board storing rejected commitments beside accepted ones.** That
  puts them one boolean away from the PPC denominator.
- **HMAC-signed OIDC (`client_secret_jwt`) and `tls_client_auth`.** Asymmetric
  algorithms only — there is no HMAC verification path to confuse.

`SECURITY.md` carries the two security items that have not been done and cannot
be done here: no penetration test, and no run against a commercial identity
provider.

---

## What this was grounded in

The forensic items follow AACE International Recommended Practice 29R-03, whose
taxonomy of nine Method Implementation Protocols separates observational
methods (3.1–3.5, which read the schedules as they are) from modelled ones
(3.6–3.9, which alter the network). `windows.py` implements MIP 3.3; R4
proposes 3.6 and 3.9.

The SCL Delay and Disruption Protocol, 2nd edition, reaches the same place from
the other direction: its second edition **removed** the first's blanket
preference for Time Impact Analysis, after that preference was repeatedly cited
to justify the method even where the results were theoretical rather than
reflective of what happened. What determines the available method is the
quality of the records — which is the argument for R1, since the records
usually arrive as files.

The competitive reading is consistent on two points: DCMA-14 quality checking
and data-date support are what separate a schedule tool from a Gantt chart, and
teams routinely run two systems because their Lean planning tool and their CPM
master schedule cannot share a model. This engine's location, takt and Last
Planner modules emit ordinary networks the same CPM schedules, which is a
deliberate answer to the second point.

Sources:

- [AACE 29R-03 Forensic Schedule Analysis Methods — Long International](https://www.long-intl.com/articles/schedule-analysis-method-2/)
- [29R-03: Forensic Schedule Analysis (recommended practice)](https://drclaim.ir/wp-content/uploads/2021/05/AACE-Recommended-Practice-Forensic-Schedule-Impact-Analysis-29R-03.pdf)
- [SCL Delay and Disruption Protocol, 2nd edition](https://www.scl.org.uk/sites/default/files/documents/SCL_Delay_Protocol_2nd_Edition_Final.pdf)
- [The SCL Protocol 2nd edition — what changed](https://www.addleshawgoddard.com/globalassets/insights/real-estate/the-scl-delay-and-disruption-protocol-2nd-edition-february-2017-updated-and-improved.pdf)
- [Harmonizing SCL D&D2 and AACE 29R-03 — Ankura](https://ankura.com/insights/harmonizing-scl-dd2-and-aace-29r-03-complementary-frameworks-for-forensic-delay-analysis-in-international-arbitration)
- [Oracle P6 Professional — import/export file formats](https://docs.oracle.com/cd/F88968_01/English/admin/p6_pro_importing_exporting/import_export_file_formats.htm)
- [XER vs XML files in Primavera P6](https://globalpm.com/xer-vs-xml-files-in-primavera-p6-which-one-should-you-use/)
- [Best CPM software 2026 — feature comparison](https://www.planera.io/post/best-cpm-software)
- [Best construction scheduling software 2026 (CPM & Lean)](https://constructioncoverage.com/construction-scheduling-software)

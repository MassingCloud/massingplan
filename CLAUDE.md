# massingplan

Read `AGENTS.md`, then `SPEC.md`. They carry the architecture, the golden rules
and the phase plan.

Three things that are easy to get wrong here and expensive to fix:

1. **`massingplan/core/` must import nothing but the standard library.** It is
   copied verbatim into `ibuilder/massing`'s source tree. One `import pydantic`
   and the vendoring stops working — and it will not fail here, it will fail in
   the other repo, weeks later.

2. **Half-open spans, absolute ordinals.** `[start, finish)`, `finish - 1` is the
   last day worked, and the conversion to a displayed date happens at exactly one
   site. Every off-by-one bug in scheduling software lives in this convention
   being applied twice, or not at all, in two different functions.

3. **Never clamp negative float, and never default silently.** Both are the
   output the planner is looking for. `max(0, free_float)` and a bare
   `except: pass` around a missing field each produce a schedule that looks
   right and is wrong.

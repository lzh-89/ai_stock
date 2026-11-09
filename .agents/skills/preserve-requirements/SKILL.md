---
name: preserve-requirements
description: Preserve confirmed requirements across optimization proposals, primary requirement documents, implementation plans, code, tests, UI acceptance, release, and archival. Use whenever consolidating or deprecating documents, implementing an optimization, translating a long proposal into a shorter specification, reviewing whether work is complete, or investigating a missed requirement. Trigger especially when a source contains lists, classifications, filters, page actions, status transitions, failure handling, effective-time rules, rollback, or multiple user-visible outcomes that could be compressed away.
---

# Preserve Requirements

Prevent silent requirement loss by maintaining an atomic, machine-checkable chain from source to acceptance. Treat prose summaries as navigation, never as substitutes for confirmed atomic requirements.

## Required workflow

### 1. Inventory sources before editing

Read every in-scope proposal, user decision, active primary requirement, implementation plan, and relevant acceptance record. Search active and expired documents for the same business terms.

Do not mark a source document expired or integrated during this step.

### 2. Extract atomic requirements before summarizing

Assign a stable requirement ID to every independently observable behavior. Split compound sentences and lists into atomic acceptance criteria. At minimum, split these dimensions when present:

- classification or filter option;
- calculation, threshold, denominator, unit, ordering, and version;
- page entry, displayed content, user action, feedback, and recovery;
- API, field, storage, status, permission, concurrency, and effective-time behavior;
- data absence, technical failure, business rejection, risk gate, and user outcome;
- history handling, monitoring, downgrade, rollback, and audit;
- positive, boundary, failure, and compatibility acceptance cases.

Never replace a list with an umbrella sentence such as “support five classifications.” Keep one criterion for each classification and separate criteria for common display or interaction behavior.

### 3. Create or update the traceability manifest

Use the format in [traceability-format.md](references/traceability-format.md). Store the manifest under `docs/requirements/` unless the repository defines another location.

For every criterion, record its source excerpt, target document locations, applicable surfaces, artifacts, tests, and acceptance evidence. Use an explicit cancellation decision when dropping a confirmed criterion. An empty mapping is a gap, not implied scope reduction.

Set the parent requirement status to the least advanced non-cancelled criterion. Never average statuses or report the most advanced child.

### 4. Integrate documents losslessly

Map each atomic criterion into the primary requirement and implementation documents before shortening narrative text. Preserve business meaning, page behavior, fields, states, boundaries, failures, formal-result impact, and acceptance rules.

After editing, reread the source independently from the target and classify every criterion as:

- fully mapped;
- changed by an explicit decision;
- explicitly cancelled;
- still pending.

Do not treat “not copied” as “not important.”

### 5. Link implementation and tests by criterion ID

Add the requirement or criterion ID to test names, test comments, fixtures, or nearby implementation comments where practical. Test the exact semantics, not merely that an endpoint returns data or a page renders.

Every criterion needs a positive test and, when meaningful, a boundary or failure test. User-visible criteria also need real browser acceptance evidence; data migration or replay criteria need real-data validation evidence.

### 6. Apply completion gates

Use only these statuses:

1. `proposed`
2. `confirmed`
3. `implementing`
4. `implemented_unverified`
5. `verified`
6. `merged`
7. `archived`
8. `cancelled`

Do not skip from `confirmed` to `verified`. Code completion is `implemented_unverified` until automated tests and all applicable acceptance surfaces pass.

Archive a source optimization document immediately after every confirmed criterion has been losslessly mapped into the active primary requirement and implementation documents. A criterion may remain `implementing` after integration, but its pending work, owner/status, acceptance rule, monitoring, and rollback must already exist in the primary documents and traceability manifest before the source is archived. Do not archive while any confirmed criterion lacks that active mapping.

### 7. Run the deterministic audit

Run:

```bash
python3 .agents/skills/preserve-requirements/scripts/audit_traceability.py <manifest.json>
```

Run it at three checkpoints:

1. after source extraction;
2. before claiming implementation complete;
3. before integrating or expiring the source document.

Treat any audit error as a completion blocker. Warnings require an explicit explanation in the handoff.

### 8. Report coverage, not confidence

Report counts and IDs for fully covered, partial, pending, and cancelled criteria. Name every remaining gap and the formal-result impact. Complete the project-required business, product, development, and user walkthroughs before declaring `verified`.

## Prohibited shortcuts

- Do not rely on memory or a final prose reread alone.
- Do not infer that passing existing tests proves source requirements were implemented.
- Do not collapse distinct classifications, states, failures, or user actions into one criterion.
- Do not accept links to files as evidence when the cited behavior is absent.
- Do not edit a manifest to match incomplete code merely to make the audit pass.
- Do not describe a designed but unavailable page, API, or operation as implemented.

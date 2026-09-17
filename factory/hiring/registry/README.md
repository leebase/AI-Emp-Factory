# Employee deployment registry

This directory is the Factory-owned concrete registry for AE-R1.2. The
canonical schema is owned by the sibling `ai-employee` repository; this
repository owns concrete roster entries and reconciliation against live
mission/config/workspace/Board facts.

## Registry records

`records/` contains canonical `DeploymentRecord` JSON for the four existing
live-bound employees plus two draft-only employees:

- `documentation-steward`
- `data-migration-factory`
- `employee-zero`
- `linux-utilities`
- `synthetic-data-story-engineer` (draft)
- `client-meeting-dashboard-builder` (draft)
- `revenue-director` (pilot, 2026-09-04), `market-demand-scout` (pilot), `chief-of-staff` (pilot, interactive), `document-publisher` (pilot, registration), `media-synthesizer` (pilot, `mission_ref` null — breaks `reconcile`), `pipeline-truth-forecast-steward` (draft), `relationship-continuity-steward` (draft). Current joined view: `adoption-matrix.md`.

Every record uses tenant `lee-installation`, a stable employee id, a mission
reference, logical workspace binding, Delivery Profile reference, Board
identity, commissioning lineage, principal reference, credential-scope
reference, and created/updated actor/timestamps.

`registry-seed.json` is Factory input, not a second deployment schema. It
contains install-local workspace paths and specification evidence. Live entries
retain source-file SHA-256 fingerprints. Draft entries carry the canonical
`ai-employee-spec/1.0` body whose deterministic digest must match the deployment
reference; their richer domain behavioral documents remain source references,
not alternate digest formats. These reconciliation inputs are not copied into
the canonical deployment record.

## Reconciliation

Run:

```bash
python3 scripts/employee_registry.py generate
python3 scripts/employee_registry.py reconcile \
  --output hiring/registry/reconciliation-report.json
```

For `record_mode: live`, the generator reads Auto-Orch mission `config.yaml`,
`state.md`, and — when present — the append-only
`lifecycle-transitions.json` ledger. The deployment record stores the ledger
*replay base* (the state the first logged transition departs from), because
Auto-Orch `employee_binding` validates the first ledger transition against the
record's `lifecycle_state` and derives the effective state by replaying the
ledger. The reconciliation report (schema
`ai-employee-registry-reconciliation/1.1`) therefore carries both
`record_lifecycle_state` (replay base) and the effective `lifecycle_state`
per record. `record_mode: draft` records are generated strictly from the
approved seed, workspace, and specification digest; they do not require or
create an Auto-Orch mission. The reconciler compares live records with
machine truth and draft records with their seed/specification authority. It
never edits the roster, mission, Board, workspace, lifecycle, or live state.

The checked-in report (regenerated 2026-08-10 after Lee's fleet disposition
direction) currently records:

- six records and zero duplicate employee, mission, or workspace bindings;
- `documentation-steward` effective `scheduled` (record replay base `paused`),
  `employee-zero` `scheduled`, `data-migration-factory` and `linux-utilities`
  `paused`, both draft employees `draft`;
- zero drift findings; the `linux-utilities` mission-health attention finding
  (last outcome failed) remains visible;
- no automatic state repair or launch authority. An untracked, hand-forged
  `records/sales-development.json` was removed 2026-08-10 as unauthoritative
  (no seed entry; its workspace binding duplicated `employee-zero`'s);
  `sales-development` remains a packet-only draft pending Lee's
  derived-output workspace approval.

The registry is identity/binding data only. Schedules, provider secrets,
mutable backlog, product content, and domain taxonomies remain owned by their
respective systems.

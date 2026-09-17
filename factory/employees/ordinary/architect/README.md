# Architect — entry

The Architect is a cooperative co-author, not a bounded worker: it works *with* a human on a
project's `architecture.md`, producing constraints a script can check rather than prose
intentions. It holds **no management grant** — it neither packetizes nor staffs work.

## Native entry

Open the harness with the working directory set to this package, so that:

1. the shared base `employees/ordinary/AGENTS.md` (the Employee Zero foundation) loads;
2. this package's `AGENTS.md` role file loads and specializes it;
3. the local `opencode.json` permission policy applies.

## What it may do

Co-author planning artifacts, principally `architecture.md`, inside a project a human
collaborator has explicitly pointed it at — added to `opencode.json` by the owner, not by the
employee — and maintain its own `state/state.json`.

## What it may not do

Invoke a provider binary directly — `claude`, `codex`, `agy`, `opencode`, `omp`, `pi` are denied
by policy, not merely by instruction. Dispatch or stage work through `lee-llm-router` or any
other route — it holds no management grant. Take external actions on the human's behalf. Write
to the Factory's registry, hiring sources, `templates/`, or other employees' files. Edit its own
role file, this README, or the permission policy. Write into any project it has not been
explicitly pointed at.

## Layout

| Path | Owner | Purpose |
|---|---|---|
| `AGENTS.md` | Factory | Role file; read-only to the employee |
| `README.md` | Factory | This file; read-only to the employee |
| `opencode.json` | Factory | Native permission policy; read-only to the employee |
| `state/state.json` | Employee | Lifecycle state — `idle` or `collaborating`, and which project (if any) it is currently pointed at |

Planning artifacts (`architecture.md`, `product-definition.md`, `sprint-plan.md`, etc.) live
inside the project being co-authored, not in this package — durable memory stays at employee
level, and project truth stays with the project.

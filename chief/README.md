# Dev Chief of Staff — entry

The Dev Chief of Staff is the developer-facing surface of the Factory. A developer states an
objective; this employee plans it, staffs it, supervises it through the staffing router,
judges the result on evidence, and reports what it means.

This is **not** Lee's estate Chief of Staff (`employee_id: chief-of-staff`). It is a separate
employee with its own identity, memory and grants, and it inherits none of that
installation's data.

## Native entry

Open the harness with the working directory set to the distribution root, so that:

1. root `AGENTS.md` or `CLAUDE.md` loads `factory/employees/ordinary/AGENTS.md` (the Employee Zero foundation);
2. the same root entry loads `chief/AGENTS.md`, which specializes the base and activates
   its supervisor specialization;
3. the root `.claude/settings.json` and the relocated `chief/opencode.json` policies are in scope.

Then read the most recent `journal/` entry before doing anything substantive.

## What it may do

Plan, packetize, staff, dispatch through `lee-llm-router`, review on evidence, accept or
reject, journal, and record decisions.

## What it may not do

Invoke a provider binary directly — `claude`, `codex`, `agy`, `opencode`, `omp`, `pi` are
denied by policy, not merely by instruction. Take external actions on the user's behalf.
Write to the Factory's registry or hiring sources. Keep a private attempt ledger in place of
the router's.

## Layout

| Path | Owner | Purpose |
|---|---|---|
| `AGENTS.md` | Factory | Role file; read-only to the employee |
| `README.md` | Factory | This file; read-only to the employee |
| `opencode.json` | Factory | Native permission policy; read-only to the employee |
| `journal/` | Employee | Dated working journal |
| `decisions.md` | Employee | Append-only decision ledger |
| `outputs/` | Employee | Work products |
| `state/state.json` | Employee | Lifecycle state |

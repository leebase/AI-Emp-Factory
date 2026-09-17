# AI-Emp-Factory

This repository is a **distribution, not a workspace**. It is assembled from pinned
component versions by a repeatable release step; nothing in it is hand-curated. Every
path outside `user/` is replaceable by the next release.

## Preconditions

Exactly four, and nothing else. Nothing is acquired at setup time, because there is no
install step:

1. **An OS** — Linux or macOS, with a POSIX shell.
2. **An installed CLI harness** — the coding-agent CLI you already use (for example
   Claude Code).
3. **Working credentials** for that harness.
4. **This repository, cloned** (see below).

Your Chief of Staff performs the configuration and shows you evidence that each
precondition is met. It does not install anything, and neither does this repository.

## Clone

    git clone git@github.com:leebase/AI-Emp-Factory.git
    cd AI-Emp-Factory

## Start here

Open a session with the working directory set to `chief/`:

    cd chief

That session is your Chief of Staff. State what you want done; it decides how, gets it
done through the governed route, checks the result on evidence, and tells you what it
means.

## Layout

| Path | Owner | Survives a release? |
|---|---|---|
| `chief/` | distribution | no — replaced by the next release |
| `factory/` | distribution | no |
| `router/` | distribution | no |
| `schemas/` | distribution | no |
| `stage-workers/` | distribution | no |
| `user/` | **you** | **yes — never touched by a release** |
| `README.md` | distribution (generated) | no |
| `MANIFEST.lock.json` | distribution (generated) | no |

## Your data

`user/` is the user-data boundary, established on day one because retrofitting it is
miserable: `journal/`, `decisions.md`, `memory/` and `projects.json` are yours. Re-running
the release step refreshes everything else and refuses to write inside `user/`.

## Provenance

`MANIFEST.lock.json` records, per component: the source, the declared pin, the resolved
commit, the paths copied, and whether unreviewed working-tree state was allowed into this
build. Components are materialized from their pinned commits (`git archive`) and copied
from that materialization, so no byte in this tree comes from a development working tree;
the release step's `--from-worktree` mode is the explicit opt-out for local iteration.
The configuration in `router/config/staffing/` is shipped verbatim, with its `@…@`
relocation tokens unresolved, so the tree works from wherever you cloned it.
| Component | Source | Commit | Files |
|---|---|---|---|
| `router` | `/home/lee/projects/lee-llm-router` | `0924d63cd8efbbb8cb40b557cb35872f47ea949f` | 76 |
| `schemas` | `/home/lee/projects/ai-employee` | `2e5547f4808b98cbfdca4a167087b550d53c7a3d` | 9 |
| `factory` | `/home/lee/projects/ai-employee-factory` | `d126a4f30b02d82e580565f193d289e975b5b697` | 12 |
| `stage-workers` | `/home/lee/projects/auto-orch` | `8f40e2ba2a9081a257a1769fa52f61815203fc86` | 4 |

Built 2026-09-17T18:40:00Z by `scripts/build_distribution.sh` from `scripts/manifest.distribution.json` (components: revision).
`user/` is owned by the user: the builder seeds it only when it is absent and
never overwrites anything inside it.

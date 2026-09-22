# AI-Emp-Factory

This repository is a **distribution, not a workspace**. It is assembled from pinned
component versions by a repeatable release step; nothing in it is hand-curated. Every
path outside `user/` is replaceable by the next release.

## Preconditions

The portable Board release targets Linux x86_64 and needs Python 3, Git, a POSIX shell,
and an already installed/authenticated coding-agent harness. Go is a build-time release
tool only; the shipped static Board binary does not require Go on the target.

## Clone

    git clone https://github.com/leebase/AI-Emp-Factory.git
    cd AI-Emp-Factory

## Install, then start here

Start with `START-HERE.md` for the interactive Chief and `/supervise` setup.
For Board installation, follow `INSTALL.md` from the distribution root:

    ./bin/install

Open a session with the working directory set to **the distribution root** — not
`chief/`. Root `AGENTS.md` and `CLAUDE.md` explicitly load the common Employee Zero
foundation and the Chief specialization. The permission policy is root-relative and
the enforcement surface lives at `.claude/`, so a session opened inside `chief/`
does not establish the supported Chief entry:

    cd AI-Emp-Factory

After installation, that root session is the Chief of Staff entry. State what you want
done; it works through the governed router and Board seams, checks the result on
evidence, and reports what it means.

## Layout

| Path | Owner | Survives a release? |
|---|---|---|
| `chief/` | distribution | no — replaced by the next release |
| `factory/` | distribution | no |
| `board/` | distribution: static binary, safe source, supported provisioners | no |
| `router/` | distribution | no |
| `schemas/` | distribution | no |
| `stage-workers/` | distribution | no |
| `bin/` | distribution entry points and generated runtime tools | no |
| `.claude/` | distribution enforcement policy | no |
| `user/` | **you** | **yes — never touched by a release** |
| `README.md` | distribution (generated) | no |
| `MANIFEST.lock.json` | distribution (generated) | no |

## Your data

`user/` is the user-data boundary, established on day one because retrofitting it is
miserable: `journal/`, `decisions.md`, `memory/` and `projects.json` are yours. Re-running
the release step refreshes everything else and refuses to write inside `user/`. The
installer places non-secret target-local Board/config state beneath `user/`; credentials
belong in protected account configuration outside the checkout.

## Provenance

`MANIFEST.lock.json` records, per component: the source, the declared pin, the resolved
commit, the paths copied, and whether unreviewed working-tree state was allowed into this
build. Components are materialized from their pinned commits (`git archive`) and copied
from that materialization, so no component byte comes from a development working tree;
the release step's `--from-worktree` mode is the explicit opt-out for local iteration.
The release-local runtime templates are separate generated packaging inputs and
their exact hashes are recorded in the lock.
The Board entry additionally records its static build command, exact source version,
toolchain path/version/hash, and binary hash. Checked distribution-layout transforms and
repository-local runtime-template input hashes are recorded separately in the lock.
The configuration in `router/config/staffing/` is shipped verbatim, with its `@…@`
relocation tokens unresolved, so the tree works from wherever you cloned it.
| Component | Source | Commit | Files |
|---|---|---|---|
| `router` | `/home/lee/projects/lee-llm-router` | `a2712af4ad9712cdeb9e15dd76eba18f3bca9875 (see lock)` | 76 |
| `schemas` | `/home/lee/projects/ai-employee` | `2e5547f4808b98cbfdca4a167087b550d53c7a3d` | 9 |
| `board` | `/home/lee/projects/agent-board` | `1f491966e5bdc98203e65d998a363f39fff650f2 (see lock)` | 94 |
| `factory` | `/home/lee/projects/ai-employee-factory` | `40ed39798e452286e52319b67ad190ab5affc78f (see lock)` | 17 |
| `stage-workers` | `/home/lee/projects/auto-orch` | `8f40e2ba2a9081a257a1769fa52f61815203fc86 (see lock)` | 4 |
| `rate-table` | `/home/lee/projects/agent-orch` | `fbedba033dc79e91e0698b5c8a788c85e5b1be2d` | 1 |

Built 2026-09-22T22:14:48Z by `scripts/build_distribution.sh` from `scripts/manifest.distribution.json` (components: revision).
`user/` is owned by the user: the builder seeds it only when it is absent and
never overwrites anything inside it.

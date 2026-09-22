# Start here: Bonnie's Chief

Open the installed AI-Emp-Factory distribution at its **repository root**. The
root loads the Chief of Staff role and its existing permission policy. Setup is
an operator action; a Chief or worker must not edit its own role or policy.

## Open your Chief

After the operator has completed and verified setup, open a terminal in the
AI-Emp-Factory folder and run:

```sh
./bin/chief-runtime start
```

Tell the Chief which project to work on and where its architecture is recorded.
It starts with Opus 5.5 medium. Your normal supervised command is:

```text
/supervise user/plans/first-change.md crew bonnie-anthropic
```

Python unittest/pytest checks can be run through the distribution interpreter after
the Chief inspects the project and agrees the test command in the plan. These
execute project code; native permission settings are not OS isolation.

## One-time operator setup

The install supplies `bin/chief-runtime` and the bundled router. First provide
an **operator-reviewed reference** rate table containing both `claude-sonnet-5`
and `claude-opus-5-5` entries with numeric input and output rates. These are reference estimates, not billed spend. The operator who prepares this entry owns its source
and date. Setup refuses a missing entry; it does not invent prices or copy
credentials. Existing Claude Code account authentication remains with the
operating account.

```sh
cd /path/to/AI-Emp-Factory
./bin/chief-runtime setup --rate-table /path/to/reviewed/rate_table.yaml
./bin/chief-runtime status
./bin/chief-runtime start
```

`start` opens the **human's interactive Chief session** at the distribution
root with Opus 5.5 Medium, matching the catalog's supervisor route. It puts the
shipped router on `PATH` and selects the installed user rate
table with `LEE_LLM_ROUTER_RATE_TABLE`. It is never a worker dispatch command.
Use a fresh Claude Code session after setup so `/supervise` is visible.

Setup creates `user/config/chief/staffing/` with an explicit Anthropic-only
catalog. It contains Sonnet 5 High for authoring and Opus 5.5 Medium for
review and supervision. The Opus route copies the shipped Claude Medium route
metadata and replaces its model id explicitly; its price comes from your
reviewed rate table. The catalog has no other provider routes and does not
reuse Lee's subscription fee as Bonnie's. The installed command passes
`--catalog-dir` on router staffing and run calls. Setup also creates a
project-local `.claude/commands/supervise.md` from the router's supported
shim renderer, relocated to `./bin/lee-llm-router`. If that command or any
generated configuration differs on rerun, setup refuses to overwrite it.
Inspect and reconcile an edited file yourself before rerunning.

The Chief, author and reviewer have separate native instructions and settings.
Author and reviewer workdirs are under `user/workers/bounded-worker/`, with
the reviewer in its own subdirectory. Their role files explicitly replace any
inherited Chief specialization. The worker cannot approve Board work, delegate
or dispatch providers; the reviewer inspects the candidate and returns a
report without editing code. Native Claude settings help enforce those roles;
they are **not an operating-system sandbox**. Keep packet owned paths narrow,
inspect the actual diff and honor the router's `--workdir` setting.

## First use

In the Chief session, try a read-only request first:

> Read the project architecture and the current source. Propose one small
> useful change with exact owned files, a test oracle, a time limit and a
> reviewer. Show me the proposed packet and the Anthropic route eligibility.
> Do not implement yet.

Once the packet is agreed, save the plan under `user/plans/` and use:

```text
/supervise user/plans/first-change.md crew bonnie-anthropic
```

The command's author and review runs must use their distinct workdirs and
report the actual router attempts, diff and tests. The initial author is Sonnet 5
High and reviewer Opus 5.5 Medium. The crew also records the reverse routes as
bounded fallbacks: review must always exclude the actual author route. Lee explicitly authorized **same-family
review for this single-provider product**. It is a second Anthropic
inspection, **not cross-family independent review**. The router still excludes
the exact author route from review, and its judge role retains the stricter
different-family rule; a judge request can therefore refuse in this catalog.
Do not weaken policy or claim a refused review passed.

Board registration and a successful setup status do not prove a full software
change has completed. The operator must observe a real author run, distinct
review, Chief verification and truthful Board close before calling the
installation end-to-end usable.

## Maintainer integration contract

The distribution builder copies `scripts/distribution/chief_runtime.py` to
`bin/chief-runtime` and this guide to `START-HERE.md`, then includes both in
its manifest. The runtime derives the distribution root from its own path and
expects the shipped router source under `router/src`, the six YAML catalog
files under `router/config/staffing`, and the normal project-local Python
dependencies from `bin/install`. No target or source policy is modified by
the builder merely by including these files.

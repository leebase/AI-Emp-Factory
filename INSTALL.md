# Install the local Board and Staff UI

This is an application install for the checked-out distribution. It does not recreate a
bare host, install OS packages, copy Lee's estate, or launch an employee or schedule.

## Preconditions

Use Ubuntu 24.04 on Linux x86_64 with Python 3, Git, and an already installed and signed-in
Claude CLI. No Go toolchain, Docker, `sudo`, or global Python install is used.

From the distribution root:

```sh
bin/install
```

The operator ID defaults to the actual OS account name. To choose the future human Board
identity explicitly, run `bin/install --operator bonnie`. This is administrative setup
performed by the operator; it is not employee-owned work.

The installer creates `.venv`, installs `requirements.txt` there, and initializes only
target-local data:

- `user/board/`: database location, artifacts, a two-person local registry, instance
  declarations, and evidence/backups;
- `user/config/`: non-secret Board and fresh-registry configuration;
- `~/.config/agent-board/`: owned mode-0700 credential directories and mode-0600 files.

Existing data is validated and preserved. Unsafe ownership, modes, symlinks, changed
configuration, or a foreign registry cause a refusal; credentials are never silently
rotated. The initial human password is written to the reported mode-0600
`operator-onboarding-password` path. Its value is never printed, passed in argv or an
environment variable, or written in the repository. Change/remove it after first login.

## Run locally

The Board is configured for `127.0.0.1:8787` only. Its mission source is explicitly
installation-local and initially empty, so no development-estate roster is inherited. Plain-HTTP cookies are enabled solely
for this literal-loopback deployment; `AGENT_BOARD_PUBLIC_READ` remains off. There is no
public listener and this procedure makes no firewall change.

When a user systemd session is available:

```sh
systemctl --user daemon-reload
bin/board-runtime start
bin/board-runtime status
bin/board-runtime stop
```

For first evidence without systemd, keep this foreground process attached:

```sh
bin/board-runtime serve
```

From an administrator workstation, access the UI through an SSH tunnel, then open
`http://127.0.0.1:8787` locally:

```sh
ssh -N -L 8787:127.0.0.1:8787 account@host
```

## Commissioning

Initialization creates fresh `commissioning` records through Factory's
`employee_registry.py`. It does not activate them and does not mint participant secrets.
The intended operator commands are explicit:

```sh
bin/board-runtime commission chief-of-staff
bin/board-runtime commission bounded-worker
# after reviewing the dry-run and prerequisite disposition:
bin/board-runtime commission chief-of-staff --execute
bin/board-runtime commission bounded-worker --execute
```

Run and inspect the dry-run first. It invokes the shipped Board filesystem and instance
preflights and makes no change. `--execute` invokes the same supported `cutover.sh`
transaction, which owns backup, Factory lifecycle activation, participant credential and
auth provisioning, binary installation, service restart, live verification, receipts,
and rollback. The Board service must already have been started explicitly and be healthy;
initialization itself never starts or enables it.

The packaged candidate remains immutable under `board/bin/`. The service runs a distinct
mutable copy under `user/board/runtime/`, allowing cutover and rollback to replace and
restore the installed binary. The wrapper checks both binaries' versions and hashes before
commissioning and supplies Factory's schema import path. A stable, employee-specific
backup directory makes an interrupted transaction visible instead of silently overwriting
it. A rerun of an active employee succeeds only after the protected credential, endpoint,
auth binding, activation evidence, and passed verification receipt all agree; otherwise it
refuses for operator reconciliation.

Literal-loopback HTTP support must be present consistently in the pinned Board
`instance-declaration.py`, `cutover.sh`, and `commission-verify.py`. Non-loopback endpoints
remain HTTPS-only. No direct SQLite edit or alternate authentication workflow is used.

## Delegation

The Chief has one fixed local manager seam. It accepts only the four reviewed helper
operations and never accepts a Board URL, credential, provider command, or generic request:

```sh
bin/board-runtime delegate create --delegator principal:bonnie \
  --assignee bounded-worker --direction-file user/direction.md \
  --title "Bounded task" --mission local --priority P1 \
  --instructions-file user/instructions.md --reviewer chief-of-staff \
  --bound-minutes 20 --decision-ref decision:local
bin/board-runtime delegate status --task-id TASK_ID
bin/board-runtime delegate record --direction-file user/direction.md \
  --task-id TASK_ID --attributed-by principal:bonnie --entry "Assigned"
bin/board-runtime delegate close --task-id TASK_ID --approve --complete
```

Direction and instruction files must already exist under ordinary `user/` work paths.
Board/config/runtime paths, external paths, symlinks and hardlinks are refused.

Network operations always target `http://127.0.0.1:8787`; the reviewed helper reads the
mode-0600 manager credential from the effective account's protected configuration root.

## Bounded worker

After `bounded-worker` is commissioned, its fixed participant CLI is:

```sh
bin/board-worker register
bin/board-worker poll
```

The executable binds the `bounded-worker` identity and
`user/workers/bounded-worker/` workspace in code. It has no identity, endpoint,
credential, workspace, provider, or arbitrary-request override. Its machine identity is
the actual lower-cased trusted hostname used during bootstrap and registration.

Perform assigned work inside that workspace and place result files beneath
`user/workers/bounded-worker/outputs/` (or bounded state evidence beneath `state/`). Then
upload and submit it for review:

```sh
bin/board-worker artifact TASK_ID KIND outputs/file
bin/board-worker review TASK_ID
```

The same CLI supports `read TASK_ID`, `list`, `renew LEASE_ID`, `ask TASK_ID MESSAGE`,
`inbox`, `ack MESSAGE_ID`, and `events TASK_ID`. Artifact paths cannot leave the worker's
own `outputs/` or `state/` trees. Submission requests independent review; it does not prove
independence or approval. Task approval and completion remain manager-owned operations and
are intentionally unavailable from `bin/board-worker`.

## Acceptance evidence

Unit tests prove bootstrap layout, idempotence, file protection, redaction, refusal of
unsafe existing state, loopback configuration, supported transaction command composition,
dry-run non-execution, failure propagation, service-unit compatibility, and the narrow
delegation and fixed worker wrappers. They do not prove live commissioning. These remain explicitly pending
until run on the target with the final pinned Board helper revision:

- `[pending]` Board health and served-version transcript;
- `[pending]` authenticated Staff UI and fresh roster readback;
- `[pending]` supported machine registration and per-instance activation;
- `[pending]` participant denial of cross-identity and manager-only operations;
- `[pending]` real delegated task/result/artifact roundtrip and truthful review state;
- `[pending]` install into a second empty application directory.

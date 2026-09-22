# Ordinary employee Board seam

`board_seam` is the shared least-privilege participant client for one
commissioned ordinary employee. A package binds its own lowercase
`employee_id` in code and constructs `BoardClient(employee_id,
workspace_root=...)`; the client derives
`/home/lee/.config/agent-board/participants/<employee_id>/`, verifies the
mode-0700 root and mode-0600 no-follow packet/credential files, and lets the
Board derive the actor from that protected credential. It never accepts an
actor, endpoint, or credential path from an operation or from the CLI.

The seam covers only the Board participation obligations: register, assigned
task reads/listing, poll/lease renewal, artifact upload from `outputs/` or
`state/`, review submission, task-scoped ask/ack, inbox, and task events. It
does not create, approve, complete, cancel, or administer tasks/machines.
Tests can inject a request callable returning parsed bodies, so no listener is
needed.

The Factory Records Clerk remains the historical fixed-client proof for now;
it will migrate to this seam in **B8**. Do not copy its credential or identity.

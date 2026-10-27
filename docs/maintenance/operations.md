# Diagnose, recover and update without replaying effects

## Worker failures

Worker errors are JSON records with `event=worker_operation_failed`, a safe
`error_class`, application stack locations and available correlation IDs.
Messages, local variables, raw URLs and provider response bodies are excluded.
Identifiers may be absent if failure happened before a run/attempt was claimed.

1. Identify the tested/deployed commit and the failing worker. Find `run_id`,
   `attempt_id`, generation and command/allocation identity if available.
2. Read `GET /runs/{id}`, `/events` and `/artifacts` with the owner's operator
   token. For an integration problem inspect the existing gateway/runner journal
   using the documented service access and readback endpoints.
3. Distinguish a program/DB/storage error from a documented NEEDS_INPUT reason.
   Fix missing configuration before resuming; do not alter a bound model profile
   silently. New profile semantics require the existing new-run path.
4. For unknown external results read the original operation identity. A lost
   CREATE/model/command response may still have caused an effect or a charge.
   Do not delete a journal, forge a receipt or release the runtime lock manually.
5. Cancellation becomes complete only after owned resources are confirmed stopped.
   Capture evidence of unresolved allocations for further recovery.

The log improvement does not create a new retry policy. Trace locations show
module/function/line, never source text or absolute host paths. A trusted
operator can correlate this with the immutable deployed source.

## Journal versions

Runner and Timeweb gateway keep separate SQLite journals. Each now records
`PRAGMA user_version = 1`. An existing unversioned journal is adopted only when
its exact table/column/key structure matches the known legacy layout. A future
or incompatible schema is rejected, not replaced by an empty journal.

The gateway makes a version-zero backup before adoption. Keep this backup
until the update has been checked. It contains resource identities and
ownership metadata; handle it as operational data, outside Git and public
artifacts. Runner restart keeps uncertain commands UNKNOWN and requires readback;
adoption is not execution permission.

Before an update:

1. Stop admitting new work. Complete or request cancellation of active work and
   obtain termination evidence; preserve UNKNOWN outcomes and their identities.
2. Stop all writers for the journal being updated; copy the database using a
   consistent SQLite backup, not only the main file while WAL is live.
3. Record binary commit, journal version and backup location. Test a copy first.
4. Start the compatible new version. Check journal version and existing resource
   identities; exercise only a permitted readback before issuing new work.
5. If schema adoption fails, stop and inspect the failure. Do not keep retrying
   by renaming/deleting the original file.

Mixed old/new components are allowed only where the unchanged `/v1` contract
and persisted formats have been checked together. Otherwise use a maintenance
window. Replacing an ephemeral runner VM still requires accounting for its
old uncertain effects; "ephemeral" does not mean safe to forget.

## Rollback

No control-plane Alembic schema change is part of this refactor. Checkpoints,
command hashes and workflow strings preserve their existing formats. Journal
v1 adoption adds version metadata and retains legacy rows.

A code rollback is not a journal restore. Never restore an older backup after
new resource effects occurred without reconciling those effects first: stale
intent data could cause duplicate creation or lose an owned VM. Use the recorded
binary/format compatibility and readback before choosing restore. If a later
migration changes actual tables, its own rollback/forward-repair procedure is
required before deployment.

## Repository gate

CI job `Maintainer checks` must pass on the exact proposed SHA. A desired main
ruleset should require PR + that check, block force pushes/deletion, and have
an explicit documented owner bypass policy if one is needed. This document
does not apply repository administration settings. Verify effective rules via
GitHub after the owner applies them; having CI YAML is not branch protection.

Live Timeweb creation/deletion, S3, provider model behavior and external network
isolation remain distinct deployment gates. Offline/unit/browser success does
not establish that those services are configured or that a release is live.

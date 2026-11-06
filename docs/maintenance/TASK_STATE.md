# Human maintainability implementation

## ACTIVE_CONTRACT

Implement M01–M10 from `specification.md` against baseline
`bea8639471537fb9c181089cdb027d69e31a041b`. Preserve public behavior, stable
identities, hashes, durable effects, state transitions and the separate runner.
No live cloud resources or production rollout are part of local implementation.

## TASK_STATE

| Task | Implementation and evidence | Acceptance state |
|---|---|---|
| M01 | Locked development tools, one check entry point, lint/type/import/schema negative controls, CI with mandatory PostgreSQL/Compose/browser checks | VERIFIED: full CI passes with no skips. Main protection policy is proposed, not applied |
| M02 | Explicit operator DTOs, generated OpenAPI, independent baseline response/schema checks | Implemented |
| M03 | Claims/fences/artifacts/limits/stage results and runner/provider ports; compatibility fixtures and typing controls | Implemented; retained legacy typing scope is explicit in pyproject |
| M04 | Composed orchestration owners, immutable stage registry, import/handoff use cases | Implemented |
| M05 | Shared transaction owner, separate state vocabularies, metadata/commands/evidence/usage owners, fencing and rollback tests | VERIFIED: both PostgreSQL checks pass in full CI |
| M06 | Structured safe failure context and original-exception correlation across operation boundaries | Implemented; fault probes check identities, state and secret omission |
| M07 | Separate runner/gateway owners; journal version 1, exact legacy adoption and gateway backup | Implemented; genuine baseline fixtures checked |
| M08 | Readable ES modules, memory-only token, selection generations and abort, eight browser scenarios | Implemented |
| M09 | Contribution, architecture, workflow sequences, change guide, ADR and recovery instructions | Implemented; current evidence in verification report |
| M10 | Independent human exercise and evidence form | PREPARED / NOT EXECUTED; an AI reviewer cannot satisfy it |

The [specification](specification.md) is the unchanged historical audit/TZ.
Its original authorization/status section describes the audit, not this later
implementation request. Current implementation status is this file and
[verification](../verification.md).

Baseline: 196 CI cases; local 194 pass, PostgreSQL/Compose not available.
Branch: `codex/human-maintainability`. No merge, production rollout or live cloud
resource creation is part of this implementation. Completion of technical checks
does not establish the M10 human maintainability result.

HISTORICAL DELIVERY: [PR 1](https://github.com/rtalyutin/v2c/pull/1), implementation
commit `50dedd5f760c9b75c6d216a35c9e4a29414a9983`.
VERIFIED: [CI run 40509217223](https://github.com/rtalyutin/v2c/actions/runs/40509217223),
270 Python cases / zero skips, 8 browser cases, PostgreSQL + SQLite schema checks,
Compose config and container build. Exact trees and job evidence are in verification.
Historical PR #1 is included in main at `0abb85c0a343b84248edfffaa8d8b844804bccb9`.
The draft/no-merge description above records the earlier delivery stage, not the
current repository status. Human M10 remains unexecuted. Current SOLID work is
tracked separately in [SOLID TASK_STATE](../solid/TASK_STATE.md).

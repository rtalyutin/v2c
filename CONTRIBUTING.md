# Working on v2c

Use Python 3.12 and Git. Browser checks use Node.js 22 and Chromium installed
by the locked Playwright version. No cloud/model credentials are needed for
the fast developer profile. Never use a production database for tests.

## Start from a clean checkout

```sh
python3 scripts/bootstrap_dev.py
. .venv/bin/activate
python scripts/check.py
```

`bootstrap_dev.py` creates `.venv`, installs the exact developer lock and the
editable project. On Windows activate `.venv/Scripts/activate` instead. The
editable build uses setuptools from the build-system constraint in pyproject;
runtime/test packages are locked separately. A compiler/build toolchain is not
guaranteed on every operating system; the validated CI profile is Ubuntu/Python 3.12.

For the API follow [README](README.md#локальный-запуск-управления). Start worker
in another terminal with the same environment. An unconfigured model/runner
causes explicit waiting, not simulated project success.

## Checks and their boundaries

| Command | Scope |
|---|---|
| `python scripts/check.py` | Ruff, types, import boundaries, docs links, OpenAPI drift, all Python tests, disposable SQLite migration/schema comparison |
| `python scripts/check.py --integration` | Same plus mandatory migrated PostgreSQL and Docker Compose; any skipped pytest case fails the gate |
| `python scripts/check.py --integration --web` | The complete CI profile, including real browser scenarios |
| `python -m pytest tests/test_engine.py -q` | A focused development iteration; not final acceptance |
| `python scripts/export_openapi.py --check` | Generated operator contract freshness |
| `python scripts/lock.py --check` | Dependency lock freshness without implicit upgrades |

Fast local pytest may report the PostgreSQL/Compose checks as skipped when
their prerequisites are absent. Report those skips explicitly. CI provides
both dependencies and rejects any skips; local success is not evidence of
production cloud isolation.

For browser checks:

```sh
npm ci
npx playwright install chromium
npm run check:web
npm run test:ui
```

If a managed environment provides a browser, set `V2C_BROWSER_EXECUTABLE` to
its absolute executable path and record the version in verification evidence.
This is an explicit test environment override, not a browser auto-download
fallback. See [browser tests](tests/browser/README.md).

For PostgreSQL create a **disposable dedicated database**, export both
`V2C_DATABASE_URL` and `V2C_TEST_POSTGRES_URL` to its SQLAlchemy URL, then run
`python -m alembic upgrade head` before the integration profile. The CI file
contains the reference service setup. Docker build and Compose config are
additional CI steps.

## Make a change

1. Read [architecture](docs/architecture.md), [workflow](docs/workflow.md) and
   the relevant row in [change guide](docs/change-guide.md).
2. State the intended behavior and invariants before changing code. Retain the
   old wire fixtures and persisted journal/checkpoint examples.
3. Make a focused diff. Keep formatting-only, behavior and data migration
   changes separately reviewable. Prefer an existing library/platform feature
   to an invented generic abstraction.
4. Update the schema/sequence and documentation when behavior or ownership
   changes. Generate OpenAPI using `python scripts/export_openapi.py`; never
   edit its JSON directly. Never regenerate golden wire fixtures just because
   a candidate differs from them.
5. Add a distinguishing positive/negative or recovery test for changed risk.
   A mock is not evidence of a live VM, S3 service, model or network firewall.
6. Run focused checks, then the required profile. Record actual failures and
   skips with the tested SHA. Use PR review before promotion to main.

## Type and import policy

New/extracted boundaries use strict typing. The mypy scope and exact retained
legacy exclusions are declared in pyproject, not hidden in an editor setting.
Persisted flexible metadata and raw ORM mapping remain JSON-shaped at their
serialization adapters; do not spread untyped dictionaries into new workflow
interfaces. Contract TypedDict definitions preserve existing JSON/null/hash
semantics rather than rewriting stored values.

`scripts/check_architecture.py` rejects prohibited runtime imports and cycles.
Type-only references are excluded. Contracts and pure workflow rules cannot
import control-plane side effects; persistence cannot call adapters or orchestration;
runner/provider processes cannot import control-plane ORM/state. Compatibility
facades are not exempt from the cycle check.

## Dependencies and migrations

`requirements-runtime.lock` is for the production image;
`requirements.lock` includes test/developer tools. The generator is
**uv 0.13.4**, installed from the developer lock, using Python 3.12 resolution.

```sh
python scripts/lock.py          # keep current pins; incorporate manifest changes
python scripts/lock.py --check  # no file changes
python scripts/lock.py --upgrade # explicit reviewable dependency upgrade
```

Review both lock diffs and run full checks after upgrades. `package-lock.json`
owns browser/dev dependencies; `npm ci` is the installation command.

Control-plane schema changes use a new Alembic revision; do not edit the initial
migration to conceal drift. Test a populated previous schema, upgrade, and
recovery. SQLite runner/gateway journals have their own compatibility policy:
see [operations](docs/maintenance/operations.md). An application rollback does
not undo a data migration.

## Definition of done

Required CI, independent review and accurate evidence precede release. For this
refactor specifically, [human acceptance](docs/maintenance/human-acceptance.md)
is a separate gate. A green suite or an AI review cannot mark it complete.
No real cloud resources are created by the local test profile.

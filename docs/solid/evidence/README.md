# Raw local evidence

Code/test candidate: `be5af35ce8915fb7164cd6efa55617ee26acebc8`.
Source/test/script/config diff from this commit was empty when final checks began.
Documentation/evidence are supplied in a following documentation-only commit.

- fast-check.log: final `python scripts/check.py`, results are authoritative.
- lock-check.log: final `python scripts/lock.py --check`.
- wheel-build.log / wheel-smoke.log: build and import from installed wheel.
- integration-preflight.log: attempted `python scripts/check.py --integration --web`;
  exit 1 for missing disposable PostgreSQL URL. Docker is also unavailable.
- web-format.log: `npm run check:web`, PASS on unchanged web files.
- browser-environment-probe.log: earlier `V2C_BROWSER_EXECUTABLE=/usr/bin/chromium
  npm run test:ui`, exit 1 before scenarios because host denied socket creation.
  This is an environment probe, not browser validation of the final candidate.
  Web assets/browser tests/Playwright config have no diff from the base commit.

All providers/credentials in test output are synthetic. No live-cloud success is
claimed. Warning: Starlette's httpx TestClient deprecation is pre-existing.

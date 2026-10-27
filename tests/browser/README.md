# Operator panel browser checks

From the repository root:

```sh
npm ci
npx playwright install --with-deps chromium
npm run check:web
npm run test:ui
```

These checks render the actual HTML/CSS/ES modules in Chromium. A loopback-only
static fixture server provides the files; Playwright intercepts application HTTP
with synthetic responses and verifies request payloads. This exercises operator
flows, races, polling, logout, downloads, network errors and the mobile layout.
It does not certify the live cloud workflow or replace Python API contract tests.

`V2C_BROWSER_EXECUTABLE` can explicitly select an already installed compatible
Chromium binary for a constrained local environment. CI uses the browser pinned
by the Playwright dependency. Record the browser version if an override is used;
there is no automatic silent fallback.

The test token is synthetic and is checked to stay out of local/session storage.
Screenshots/traces under `test-results/` are disposable debugging artifacts.

import { expect, test } from "@playwright/test";

const TOKEN = "browser-synthetic-operator-token";
const DIGEST = "a".repeat(64);
const projects = [
  { id: "p-one", name: "First project" },
  { id: "p-two", name: "Second project" },
];

function run(id = "r-one", status = "AWAITING_PLAN_APPROVAL") {
  return {
    id,
    status,
    final_digest: DIGEST,
    reason: null,
    steps: [],
    usage: [],
  };
}

async function fixture(page) {
  const calls = [];
  const runs = { "r-one": run(), "r-two": run("r-two", "NEEDS_INPUT") };
  await page.route("**/*", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith("/projects") && !path.startsWith("/runs"))
      return route.continue();
    expect(request.headers().authorization).toBe(`Bearer ${TOKEN}`);
    const method = request.method();
    const body = request.headers()["content-type"]?.includes("application/json")
      ? request.postDataJSON()
      : null;
    calls.push({ path, method, body });
    let result;
    if (path === "/projects") {
      result = method === "POST" ? { id: "p-new", name: body.name } : projects;
    } else if (path.endsWith("/snapshots")) {
      result = [
        { id: path.includes("p-two") ? "s-two" : "s-one", digest: DIGEST },
      ];
    } else if (path.endsWith("/imports")) {
      result = { id: "s-new", digest: DIGEST };
    } else if (path.startsWith("/projects/") && path.endsWith("/runs")) {
      result = method === "POST" ? runs["r-one"] : Object.values(runs);
    } else {
      const [, , id, operation, artifact] = path.split("/");
      if (!operation) result = runs[id];
      else if (operation === "plan")
        result = {
          revision: 2,
          snapshot_id: "s-one",
          plan: { summary: "Example plan" },
        };
      else if (operation === "events") result = [];
      else if (operation === "artifacts" && artifact) {
        return route.fulfill({
          status: 200,
          contentType: "application/zip",
          body: "synthetic zip",
        });
      } else if (operation === "artifacts") {
        result = [
          {
            id: "a-one",
            kind: "bundle",
            sha256: DIGEST,
            manifest: { status: "CANDIDATE" },
          },
        ];
      } else if (operation === "decisions") {
        runs[id].status = body.approved ? "QUEUED" : "NEEDS_INPUT";
        result = runs[id];
      } else if (operation === "cancel") {
        runs[id].status = "CANCELLED";
        result = runs[id];
      } else if (operation === "resume") {
        runs[id].status = "QUEUED";
        result = runs[id];
      } else if (operation === "acceptance")
        result = { id: "step-handoff", status: "PENDING" };
      else if (operation === "handoff") {
        runs[id].status = body.passed ? "READY" : "NEEDS_INPUT";
        result = runs[id];
      } else throw new Error(`Unexpected request ${path}`);
    }
    await route.fulfill({ json: result });
  });
  await page.goto("/");
  return { calls, runs };
}

async function login(page) {
  await page.locator("#token").fill(TOKEN);
  await page.locator("#connect").click();
  await expect(page.locator("#workspace")).toBeVisible();
}

async function selectFirstRun(page) {
  await page
    .getByRole("button", { name: "First project", exact: true })
    .click();
  await page.locator("#runs button").first().click();
  await expect(page.locator("#run-status")).toHaveText(
    "AWAITING_PLAN_APPROVAL",
  );
}

async function openDetails(page, childId) {
  await page.locator(`#${childId}`).evaluate((el) => {
    el.closest("details").open = true;
  });
}

test("operator actions retain wire payloads, download and token stays in memory", async ({
  page,
}) => {
  const { calls } = await fixture(page);
  await login(page);
  await expect(page.locator("#token")).toHaveValue("");
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  await page.locator("#project-name").fill("Created project");
  await page.locator("#create-project").click();
  await expect(page.locator("#project-title")).toHaveText("Created project");
  await page.locator("#archive").setInputFiles({
    name: "input.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("test archive"),
  });
  await page.locator("#import").click();
  await expect(page.locator("#notice")).toHaveText("Снимок зафиксирован.");
  await page.locator("#start").click();
  await expect(page.locator("#run-status")).toHaveText(
    "AWAITING_PLAN_APPROVAL",
  );
  await openDetails(page, "approve");
  await page.locator("#approve").click();
  await expect(page.locator("#run-status")).toHaveText("QUEUED");
  await expect(page.locator("#approve")).toBeDisabled();
  await openDetails(page, "accept-task");
  await page.locator("#accept-task").fill("Add a diagnostic field");
  await page
    .locator("#accept-checks")
    .fill('[{"argv":["python","-m","pytest"],"cwd":"."}]');
  await page.locator("#acceptance").click();
  await expect(page.locator("#notice")).toHaveText(
    "Контрольное задание принято.",
  );
  await openDetails(page, "handoff-notes");
  await page
    .locator("#handoff-notes")
    .fill("Independent human verified the change.");
  await page.locator("#handoff-pass").click();
  await expect(page.locator("#run-status")).toHaveText("READY");
  await expect(page.locator("#cancel")).toBeDisabled();
  const download = page.waitForEvent("download");
  await page.locator("#artifacts button").click();
  expect((await download).suggestedFilename()).toBe("v2c-a-one.zip");
  expect(calls.find((call) => call.path.endsWith("/decisions")).body).toEqual({
    snapshot_id: "s-one",
    plan_revision: 2,
    approved: true,
  });
  expect(calls.find((call) => call.path.endsWith("/handoff")).body).toEqual({
    final_digest: DIGEST,
    passed: true,
    evidence: "Independent human verified the change.",
  });
  expect(
    calls.find(
      (call) => call.path === "/projects/p-new/runs" && call.method === "POST",
    ).body,
  ).toMatchObject({
    snapshot_id: "s-one",
    idempotency_key: expect.any(String),
  });
  await page.locator("#logout").click();
  await expect(page.locator("#workspace")).toBeHidden();
  await expect(page.locator("#login")).toBeVisible();
});

test("repository import, reject, resume, failed handoff and cancel remain available", async ({
  page,
}) => {
  const { calls } = await fixture(page);
  await login(page);
  await selectFirstRun(page);
  await openDetails(page, "repositories");
  await page
    .locator("#repositories")
    .fill(
      '[{"name":"repo","url":"https://example.test/repo.git","revision":"' +
        "b".repeat(40) +
        '"}]',
    );
  await page.locator("#import-repos").click();
  await expect(page.locator("#notice")).toHaveText(
    "Комплект репозиториев зафиксирован.",
  );
  await openDetails(page, "reject");
  await page.locator("#reject").click();
  await expect(page.locator("#run-status")).toHaveText("NEEDS_INPUT");
  await page.locator("#resume").click();
  await expect(page.locator("#run-status")).toHaveText("QUEUED");
  await expect(page.locator("#resume")).toBeDisabled();
  await openDetails(page, "handoff-notes");
  await page
    .locator("#handoff-notes")
    .fill("Independent handoff check failed.");
  await page.locator("#handoff-fail").click();
  await expect(page.locator("#run-status")).toHaveText("NEEDS_INPUT");
  await page.locator("#cancel").click();
  await expect(page.locator("#run-status")).toHaveText("CANCELLED");
  await expect(page.locator("#cancel")).toBeDisabled();
  expect(
    calls.find((call) => call.path.endsWith("/decisions")).body.approved,
  ).toBe(false);
  expect(calls.find((call) => call.path.endsWith("/handoff")).body.passed).toBe(
    false,
  );
});

test("late project response cannot replace current snapshots", async ({
  page,
}) => {
  await fixture(page);
  let release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  let entered;
  const waiting = new Promise((resolve) => {
    entered = resolve;
  });
  await page.route("**/projects/p-one/snapshots", async (route) => {
    entered();
    await delayed;
    await route.fulfill({ json: [{ id: "s-obsolete", digest: DIGEST }] });
  });
  await login(page);
  await page
    .getByRole("button", { name: "First project", exact: true })
    .click();
  await waiting;
  await page
    .getByRole("button", { name: "Second project", exact: true })
    .click();
  await expect(page.locator("#snapshots")).toHaveValue("s-two");
  const oldResponse = page.waitForResponse("**/projects/p-one/snapshots");
  release();
  await oldResponse;
  await expect(page.locator("#snapshots")).toHaveValue("s-two");
  await expect(page.locator("#project-title")).toHaveText("Second project");
});

test("late run response cannot overwrite a newly selected run", async ({
  page,
}) => {
  await fixture(page);
  await login(page);
  await selectFirstRun(page);
  let release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  let entered;
  const waiting = new Promise((resolve) => {
    entered = resolve;
  });
  await page.route("**/runs/r-one", async (route) => {
    entered();
    await delayed;
    await route.fulfill({ json: run("r-one", "FAILED") });
  });
  await page.locator("#refresh").click();
  await waiting;
  await page.locator("#runs button").nth(1).click();
  await expect(page.locator("#run-status")).toHaveText("NEEDS_INPUT");
  const oldResponse = page.waitForResponse("**/runs/r-one");
  release();
  await oldResponse;
  await expect(page.locator("#run-status")).toHaveText("NEEDS_INPUT");
  await expect(page.locator("#resume")).toBeEnabled();
});

test("logout aborts in-flight polling and schedules no further requests", async ({
  page,
}) => {
  const { calls } = await fixture(page);
  await page.clock.install();
  await login(page);
  await selectFirstRun(page);
  let release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  let entered;
  const waiting = new Promise((resolve) => {
    entered = resolve;
  });
  await page.route("**/runs/r-one", async (route) => {
    entered();
    await delayed;
    await route.fulfill({ json: run("r-one", "READY") }).catch(() => {});
  });
  await page.clock.fastForward(5001);
  await waiting;
  await page.locator("#logout").click();
  const count = calls.length;
  release();
  await page.clock.fastForward(20000);
  await expect(page.locator("#workspace")).toBeHidden();
  await expect(page.locator("#run-card")).toBeHidden();
  await expect(page.locator("#notice")).toHaveText("Сеанс завершён.");
  expect(calls.length).toBe(count);
});

test("network failure is visible and does not re-enable a forbidden action", async ({
  page,
}) => {
  await fixture(page);
  await login(page);
  await selectFirstRun(page);
  await page.route("**/runs/r-one/events", (route) => route.abort("failed"));
  await page.locator("#refresh").click();
  await expect(page.locator("#notice")).toHaveClass("error");
  await expect(page.locator("#resume")).toBeDisabled();
  await expect(page.locator("#refresh")).toBeEnabled();
});

test("mobile panel keeps content within viewport", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page);
  await login(page);
  await selectFirstRun(page);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await expect(page.locator("#run-card")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("mobile.png"),
    fullPage: true,
  });
});

test("late create-run result does not navigate away from a newer selection", async ({
  page,
}) => {
  await fixture(page);
  await login(page);
  await selectFirstRun(page);
  let release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  let entered;
  const waiting = new Promise((resolve) => {
    entered = resolve;
  });
  await page.route("**/projects/p-one/runs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    entered();
    await delayed;
    await route.fulfill({ json: run("r-one", "QUEUED") });
  });
  await page.locator("#start").click();
  await waiting;
  await page.locator("#runs button").nth(1).click();
  await expect(page.locator("#run-status")).toHaveText("NEEDS_INPUT");
  const oldResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/projects/p-one/runs") &&
      response.request().method() === "POST",
  );
  release();
  await oldResponse;
  await expect(page.locator("#run-status")).toHaveText("NEEDS_INPUT");
});

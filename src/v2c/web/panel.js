import { createApiClient } from "./web/api-client.js";
import { PanelState } from "./web/state.js";
import { createPoller } from "./web/polling.js";
import {
  element,
  notice,
  renderSession,
  renderProjects,
  renderSelection,
  renderProjectData,
  renderButtons,
  renderRun,
  saveDownload,
} from "./web/render.js";

const client = createApiClient();
const state = new PanelState();
const pending = new Set();

function showError(error) {
  if (error.name !== "AbortError") notice(error.message, true);
}

// Event handlers also guard errors and button state against obsolete selections.
function action(id, callback) {
  element(id).onclick = async () => {
    const scope = state.scope();
    pending.add(id);
    element(id).disabled = true;
    try {
      await callback();
    } catch (error) {
      if (id === "connect" || state.current(scope)) showError(error);
    } finally {
      pending.delete(id);
      element(id).disabled = false;
      renderButtons(state.run, pending);
    }
  };
}

async function refreshProjects() {
  const scope = state.scope();
  const projects = await client.json("/projects");
  if (state.current(scope, "session")) renderProjects(projects, selectProject);
}

async function selectProject(project) {
  state.selectProject(project);
  renderSelection(project);
  renderButtons(null, pending);
  const scope = state.scope();
  try {
    await refreshProject();
  } catch (error) {
    if (state.current(scope, "project")) showError(error);
  }
}

async function refreshProject() {
  if (!state.project) return;
  const scope = state.scope();
  const id = state.project.id;
  const read = ++state.projectRead;
  const [snapshots, runs] = await Promise.all([
    client.json(`/projects/${id}/snapshots`),
    client.json(`/projects/${id}/runs`),
  ]);
  if (state.current(scope, "project") && read === state.projectRead) {
    renderProjectData(snapshots, runs, selectRun);
  }
}

async function selectRun(id) {
  state.selectRun(id);
  element("run-card").hidden = true;
  renderButtons(null, pending);
  const scope = state.scope();
  try {
    await refreshRun();
  } catch (error) {
    if (state.current(scope)) showError(error);
  }
}

async function refreshRun() {
  if (!state.runId) return;
  const scope = state.scope();
  const id = state.runId;
  const read = ++state.runRead;
  const [run, plan, events, artifacts] = await Promise.all([
    client.json(`/runs/${id}`),
    client.json(`/runs/${id}/plan`),
    client.json(`/runs/${id}/events`),
    client.json(`/runs/${id}/artifacts`),
  ]);
  if (!state.current(scope) || read !== state.runRead) return;
  state.run = run;
  state.plan = plan;
  renderRun(run, plan, events, artifacts, async (artifact) => {
    try {
      const blob = await client.download(
        `/runs/${id}/artifacts/${artifact.id}`,
      );
      if (state.current(scope)) saveDownload(blob, artifact.id);
    } catch (error) {
      if (state.current(scope)) showError(error);
    }
  });
  renderButtons(run, pending);
}

const poller = createPoller(async () => {
  const scope = state.scope();
  try {
    await refreshRun();
  } catch (error) {
    if (state.current(scope)) showError(error);
  }
}, showError);

function requireProject() {
  if (!state.project) throw new Error("Выберите проект");
  return state.project;
}

function requireRun() {
  if (!state.run) throw new Error("Выберите запуск");
  return state.run;
}

async function mutateRun(operation, body, message) {
  const run = requireRun();
  const scope = state.scope();
  await client.json(`/runs/${run.id}/${operation}`, { method: "POST", body });
  if (!state.current(scope)) return;
  if (message) notice(message);
  await refreshRun();
}

async function importSource(body, headers, message) {
  const project = requireProject();
  const scope = state.scope();
  await client.json(`/projects/${project.id}/imports`, {
    method: "POST",
    body,
    headers,
  });
  if (!state.current(scope, "project")) return;
  notice(message);
  await refreshProject();
}

action("connect", async () => {
  poller.stop();
  state.reset();
  client.beginSession(element("token").value);
  const scope = state.scope();
  try {
    await refreshProjects();
    if (!state.current(scope, "session")) return;
    element("token").value = "";
    renderSelection(null);
    renderSession(true);
    notice("Подключено.");
    poller.start();
  } catch (error) {
    client.endSession();
    throw error;
  }
});

action("logout", () => {
  poller.stop();
  client.endSession();
  state.reset();
  element("token").value = "";
  renderSelection(null);
  renderSession(false);
  notice("Сеанс завершён.");
});

action("create-project", async () => {
  const scope = state.scope();
  const project = await client.json("/projects", {
    method: "POST",
    body: { name: element("project-name").value },
  });
  if (!state.current(scope)) return;
  element("project-name").value = "";
  await refreshProjects();
  if (state.current(scope)) await selectProject(project);
});

action("import", async () => {
  const file = element("archive").files[0];
  if (!file) throw new Error("Выберите архив");
  await importSource(
    file,
    { "Content-Type": "application/octet-stream" },
    "Снимок зафиксирован.",
  );
});
action("import-repos", () =>
  importSource(
    { repositories: JSON.parse(element("repositories").value) },
    undefined,
    "Комплект репозиториев зафиксирован.",
  ),
);
action("start", async () => {
  const project = requireProject();
  const snapshot = element("snapshots").value;
  if (!snapshot) throw new Error("Загрузите исходный проект");
  const scope = state.scope();
  const run = await client.json(`/projects/${project.id}/runs`, {
    method: "POST",
    body: { snapshot_id: snapshot, idempotency_key: crypto.randomUUID() },
  });
  if (!state.current(scope)) return;
  await refreshProject();
  if (state.current(scope)) await selectRun(run.id);
});
action("refresh", refreshRun);
action("resume", () => mutateRun("resume"));
action("cancel", () =>
  mutateRun(
    "cancel",
    undefined,
    "Остановка запрошена. Статус изменится после подтверждения.",
  ),
);

function decide(approved) {
  if (!state.plan) throw new Error("План не подготовлен");
  return mutateRun("decisions", {
    snapshot_id: state.plan.snapshot_id,
    plan_revision: state.plan.revision,
    approved,
  });
}
action("approve", () => decide(true));
action("reject", () => decide(false));
action("acceptance", () =>
  mutateRun(
    "acceptance",
    {
      task: element("accept-task").value,
      checks: JSON.parse(element("accept-checks").value),
    },
    "Контрольное задание принято.",
  ),
);

function human(passed) {
  const run = requireRun();
  if (!run.final_digest) throw new Error("Конечная версия ещё не подготовлена");
  return mutateRun("handoff", {
    final_digest: run.final_digest,
    passed,
    evidence: element("handoff-notes").value,
  });
}
action("handoff-pass", () => human(true));
action("handoff-fail", () => human(false));
renderButtons(null, pending);

/** DOM rendering only: transport and selection policy live in separate modules. */
export const element = (id) => document.getElementById(id);

export function notice(message, error = false) {
  element("notice").textContent = message;
  element("notice").className = error ? "error" : "";
}

function node(tag, text, className) {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = text;
  if (className) value.className = className;
  return value;
}

export function renderSession(connected) {
  element("login").hidden = connected;
  element("workspace").hidden = !connected;
}

export function renderProjects(projects, select) {
  element("projects").replaceChildren();
  for (const project of projects) {
    const button = node("button", project.name, "item");
    button.onclick = () => select(project);
    element("projects").append(button);
  }
}

export function renderSelection(project) {
  element("run-card").hidden = true;
  element("project-title").textContent = project?.name || "Выберите проект";
  element("project-actions").hidden = !project;
  element("snapshots").replaceChildren();
  element("runs").replaceChildren();
}

export function renderProjectData(snapshots, runs, selectRun) {
  const selectedSnapshot = element("snapshots").value;
  element("snapshots").replaceChildren();
  for (const snapshot of snapshots) {
    const option = node(
      "option",
      `${snapshot.id.slice(0, 8)} · ${snapshot.digest.slice(0, 12)}`,
    );
    option.value = snapshot.id;
    element("snapshots").append(option);
  }
  if (snapshots.some((snapshot) => snapshot.id === selectedSnapshot)) {
    element("snapshots").value = selectedSnapshot;
  }
  element("runs").replaceChildren();
  for (const run of runs) {
    const button = node(
      "button",
      `${run.id.slice(0, 8)} · ${run.status}`,
      "item",
    );
    button.onclick = () => selectRun(run.id);
    element("runs").append(button);
  }
}

export function renderButtons(run, pending) {
  const unavailable = {
    refresh: !run,
    resume: run?.status !== "NEEDS_INPUT",
    cancel: !run || ["READY", "FAILED", "CANCELLED"].includes(run.status),
    approve: run?.status !== "AWAITING_PLAN_APPROVAL",
    reject: run?.status !== "AWAITING_PLAN_APPROVAL",
    acceptance: !run,
    "handoff-pass": !run,
    "handoff-fail": !run,
  };
  for (const [id, disabled] of Object.entries(unavailable)) {
    element(id).disabled = disabled || pending.has(id);
  }
}

export function renderRun(run, plan, events, artifacts, download) {
  element("run-card").hidden = false;
  element("run-status").textContent = run.status;
  element("run-reason").textContent = run.reason || run.last_error || "";
  element("step-list").replaceChildren();
  for (const step of run.steps || []) {
    element("step-list").append(
      node(
        "p",
        `${step.kind} · ${step.status} · попытки ${step.attempts_used}/5`,
      ),
    );
  }
  element("usage").textContent = JSON.stringify(run.usage || [], null, 2);
  element("plan").textContent =
    plan === null ? "План ещё не подготовлен." : JSON.stringify(plan, null, 2);
  element("events").textContent = JSON.stringify(events, null, 2);
  element("artifacts").replaceChildren();
  for (const artifact of artifacts) {
    const button = node(
      "button",
      `${artifact.kind} · ${artifact.manifest?.status || "свидетельство"} · ${artifact.sha256.slice(0, 12)}`,
      "secondary",
    );
    button.onclick = () => download(artifact);
    element("artifacts").append(button);
  }
}

export function saveDownload(blob, artifactId) {
  const url = URL.createObjectURL(blob);
  const link = node("a");
  link.href = url;
  link.download = `v2c-${artifactId}.zip`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

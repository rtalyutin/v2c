/** Selection identity and read versions prevent late responses replacing a new view. */
export class PanelState {
  session = 0;
  projectVersion = 0;
  runVersion = 0;
  projectRead = 0;
  runRead = 0;
  project = null;
  runId = null;
  run = null;
  plan = null;

  reset() {
    this.session += 1;
    this.selectProject(null);
  }

  selectProject(project) {
    this.project = project;
    this.projectVersion += 1;
    this.selectRun(null);
  }

  selectRun(id) {
    this.runId = id;
    this.run = null;
    this.plan = null;
    this.runVersion += 1;
  }

  scope() {
    return {
      session: this.session,
      project: this.projectVersion,
      run: this.runVersion,
    };
  }

  current(scope, level = "run") {
    return (
      scope.session === this.session &&
      (level === "session" || scope.project === this.projectVersion) &&
      (level !== "run" || scope.run === this.runVersion)
    );
  }
}

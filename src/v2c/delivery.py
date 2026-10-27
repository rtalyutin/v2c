"""Candidate handover packages with project-scoped skills and verifiable provenance."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .intake import (
    ImportLimits,
    IntakeError,
    Snapshot,
    _git_environment,
    _inventory,
    git_command,
    inspect_snapshot,
    safe_relative_path,
)
from .storage import ArtifactRef, ArtifactStore


class DeliveryError(ValueError):
    pass


class SecretScanError(DeliveryError):
    def __init__(self, paths: list[str]):
        super().__init__("EXPORT_SECRET_DETECTED")
        self.paths = paths  # source values deliberately never included


_SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})|"
    rb"https?://[^\s/:]+:[^\s/@]+@",
    re.I,
)
_ASSIGNMENT = re.compile(
    rb"(?:^|\n)\s*[\"']?(?:[A-Z_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z_]*)"
    rb"[\"']?\s*[:=]\s*[\"']?([^\s\"'#,;}]{8,})",
    re.I,
)
_PLACEHOLDER = re.compile(
    rb"^(?:example|sample|placeholder|changeme|change_me|replace_me|your[_-]|test[_-]|dummy|x{8}|\$|<)",
    re.I,
)


def contains_secret(data: bytes) -> bool:
    return bool(
        _SECRET.search(data)
        or any(not _PLACEHOLDER.match(m[1]) for m in _ASSIGNMENT.finditer(data))
    )


def scan_secrets(root: Path, *, include_git_history: bool = False) -> list[str]:
    """Heuristic blocking scan, not a guarantee that arbitrary secrets are absent."""
    findings = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts or not path.is_file() or path.is_symlink():
            continue
        if contains_secret(path.read_bytes()):
            findings.append(path.relative_to(root).as_posix())
    if include_git_history:
        for line in git_command(root, "rev-list", "--objects", "--all").splitlines():
            sha = line.split(" ", 1)[0]
            kind = git_command(root, "cat-file", "-t", sha)
            if kind == "blob":
                # Source blobs are bounded at intake, but reject injected history too.
                size = int(git_command(root, "cat-file", "-s", sha))
                if size > ImportLimits().max_file_bytes:
                    raise DeliveryError("GIT_HISTORY_LIMIT_EXCEEDED")
                import subprocess

                body = subprocess.run(
                    ["git", "-C", str(root), "cat-file", "blob", sha],
                    env=_git_environment(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=15,
                ).stdout
                if contains_secret(body):
                    findings.append("git-history/" + sha)
    return findings


@dataclass(frozen=True)
class DeliveryBundle:
    path: Path
    manifest: dict
    artifact: ArtifactRef | None = None
    status: str = "CANDIDATE"

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "manifest": self.manifest,
            "status": self.status,
            "artifact": self.artifact.to_dict() if self.artifact else None,
        }


def _write_json(path: Path, value: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _delivery_path(
    value: str, package: Path, repository_names: set[str], *, directory: bool = False
) -> str:
    """Translate only known runner repository prefixes into package-relative paths."""
    mapped = value
    if value == ".":
        mapped = "."
    elif value.startswith("repositories/"):
        path = safe_relative_path(value)
        parts = path.split("/")
        if len(parts) < 2 or parts[1] not in repository_names:
            raise DeliveryError("DELIVERY_UNKNOWN_REPOSITORY_PATH")
        mapped = "code/" + "/".join(parts[1:])
    elif directory:
        mapped = safe_relative_path(value)
    else:
        return value
    target = (package / mapped).resolve()
    if not target.is_relative_to(package.resolve()):
        raise DeliveryError("DELIVERY_PATH_OUTSIDE_PACKAGE")
    if directory and not target.is_dir():
        raise DeliveryError("DELIVERY_COMMAND_DIRECTORY_MISSING")
    return mapped


def _portable_passport(
    value: object, package: Path, repository_names: set[str], *, key: str = ""
) -> object:
    """Keep descriptions intact while rewriting structured path values and argv paths."""
    if isinstance(value, dict):
        return {
            name: _portable_passport(item, package, repository_names, key=name)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_portable_passport(item, package, repository_names, key=key) for item in value]
    if isinstance(value, str):
        return _delivery_path(value, package, repository_names, directory=key == "cwd")
    return value


def _command_lines(commands: dict) -> list[str]:
    lines = []
    for purpose, command_values in commands.items():
        if isinstance(command_values, str):
            command_values = [command_values]
        if isinstance(command_values, dict):
            command_values = [command_values]
        for value in command_values:
            evidence, cwd = "", "."
            if isinstance(value, dict):
                evidence = " · evidence: " + str(value.get("evidence_id", "не указано"))
                cwd = value.get("cwd", ".")
                value = value.get("argv", [])
            command = shlex.join(str(v) for v in value) if isinstance(value, list) else str(value)
            if command:
                lines.append(f"- {purpose}: `cd {shlex.quote(cwd)} && {command}`{evidence}")
    return lines


_SKILLS = {
    "onboard": (
        "Войти в проект",
        "Прочитай карту системы и паспорт среды; найди точки входа и воспроизведи проект в своей тестовой среде.",
    ),
    "change": (
        "Внести изменение",
        "Уточни сохраняемое поведение, найди затронутые файлы по карте и примеру, внеси небольшой патч и запусти относящиеся проверки.",
    ),
    "debug": (
        "Исправить ошибку",
        "Воспроизведи конкретный сбой на тестовых данных, сопоставь его с проверками и запиши причину до исправления.",
    ),
    "verify": (
        "Проверить изменение",
        "Сверь точную версию файлов и выполни команды проверок. Отдели пройденные проверки от неизвестного и от человеческой приёмки.",
    ),
    "release": (
        "Подготовить выпуск",
        "Проверь рецепт среды, миграции и возможность восстановления. Подготовь выпуск и план отката; публикация требует решения владельца.",
    ),
    "update-knowledge": (
        "Обновить знания",
        "После изменения сверь карту, команды и примеры с кодом. Обнови только затронутые предпосылки и документы, затем повтори зависимые проверки.",
    ),
}


def _skills(root: Path, snapshot: Snapshot, project_manifest: dict, packaged_files: set[str]):
    skills = root / "project-skills"
    commands = _command_lines(project_manifest.get("commands", {}))
    examples = []
    for example in project_manifest.get("examples", []):
        if not isinstance(example, dict):
            continue
        path = safe_relative_path(str(example.get("file", "")))
        if path not in packaged_files:
            raise DeliveryError("SKILL_EXAMPLE_NOT_IN_DELIVERY")
        examples.append(f"- [{path}](../../code/{path}): {example.get('description', '')}")
    references = [
        f"- [{r.name}/{f['path']}](../../code/{r.name}/{f['path']})"
        for r in snapshot.repositories
        for f in r.files[:3]
        if f"{r.name}/{f['path']}" in packaged_files
    ]
    for name, (title, instruction) in _SKILLS.items():
        directory = skills / name
        directory.mkdir(parents=True)
        text = (
            f"---\nname: {name}-{snapshot.project_id}\ndescription: {title} только в проекте {snapshot.project_id}.\n---\n\n"
            f"# {title}\n\n{instruction}\n\n"
            f"Область: проект `{snapshot.project_id}`, snapshot `{snapshot.snapshot_id}`. "
            "Применять к этому проекту. После новых коммитов проверь затронутые файлы, команды и зависимости; "
            "сам факт нового commit не делает все знания недействительными.\n\n"
            "## Опорные документы\n\n"
            "- [Карта системы](../../docs/system-map.json)\n"
            "- [Среда и команды](../../docs/environment.json)\n"
            "- [Проверки и ограничения](../../docs/verification.json)\n\n"
            "## Команды из паспорта проекта\n\nВыполняй каждую строку из корня распакованной поставки, где находятся code, docs и project-skills.\n\n"
            + (
                "\n".join(commands)
                if commands
                else "Команды ещё не подтверждены; запроси рецепт воспроизведения и проверку в изоляции."
            )
            + "\n\n## Примеры в переданном коде\n\n"
            + "\n".join(examples or references)
            + "\n\nРаботай локально или в среде владельца; сервер преобразователя не требуется. "
            "Текст навыка не подтверждает успешное исполнение команд или безопасность выпуска.\n"
        )
        (directory / "SKILL.md").write_text(text)
    (skills / "README.md").write_text(
        "# Навыки проекта\n\nКаждая папка содержит SKILL.md и относится только к переданному проекту. "
        "Сохраняй расположение code/docs рядом с project-skills для относительных ссылок. "
        "Совместимость с конкретным агентом проверяется отдельно.\n"
    )


def package_delivery(
    snapshot: Snapshot,
    destination: Path | str,
    *,
    project_manifest: dict,
    verification: dict,
    store: ArtifactStore | None = None,
    original_snapshot: Snapshot | None = None,
) -> DeliveryBundle:
    """Publish only a candidate; acceptance remains controller-owned and outside code.

    `project_manifest` is a trusted controller passport, never a project PASS file.
    For complete source/final Git provenance provide original_snapshot. Missing
    passport fields remain visible limitations rather than fabricated knowledge.
    """
    inspect_snapshot(snapshot)
    if original_snapshot:
        inspect_snapshot(original_snapshot)
        if original_snapshot.project_id != snapshot.project_id:
            raise DeliveryError("PROJECT_PROVENANCE_MISMATCH")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise DeliveryError("DELIVERY_DESTINATION_EXISTS")
    limitations = list(project_manifest.get("limitations", []))
    if not project_manifest.get("commands"):
        limitations.append("Build/test commands have not been provided with execution evidence.")
    if not original_snapshot:
        limitations.append(
            "Original source history was not separately provided; bundle contains supplied snapshot history."
        )
    limitations.append(
        "Agent-environment compatibility and independent human maintenance acceptance require separate evidence."
    )
    private_paths = {safe_relative_path(p) for p in project_manifest.get("private_paths", [])}
    original_repos = (
        {r.name: r for r in original_snapshot.repositories} if original_snapshot else {}
    )
    with tempfile.TemporaryDirectory(prefix=".delivery-", dir=destination.parent) as temporary:
        workspace = Path(temporary)
        package = workspace / "package"
        package.mkdir()
        packaged_files, provenance = set(), []
        for repository in snapshot.repositories:
            source = snapshot.root / repository.path
            findings = scan_secrets(source)
            if findings:
                raise SecretScanError(findings)
            code = package / "code" / repository.name
            code.mkdir(parents=True)
            for entry in repository.files:
                relative = f"{repository.name}/{entry['path']}"
                if relative in private_paths or any(
                    relative.startswith(p + "/") for p in private_paths
                ):
                    continue
                path = code / entry["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / entry["path"], path)
                path.chmod(0o755 if entry.get("executable", False) else 0o644)
                packaged_files.add(relative)
            history = workspace / ("history-" + repository.name)
            history.mkdir()
            previous = original_repos.get(repository.name)
            if previous:
                # Only intake-created internal metadata may reach this function.
                shutil.copytree(original_snapshot.root / previous.path / ".git", history / ".git")
                source_sha = previous.source_sha
            else:
                shutil.copytree(source / ".git", history / ".git")
                source_sha = repository.source_sha
            for path in code.rglob("*"):
                if path.is_file():
                    target = history / path.relative_to(code)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
                    target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
            git_command(history, "--literal-pathspecs", "add", "--force", "--all", "--", ".")
            if git_command(history, "status", "--porcelain"):
                git_command(
                    history,
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "Prepared project delivery",
                )
            final_sha = git_command(history, "rev-parse", "HEAD")
            git_command(history, "update-ref", "refs/v2c/source", source_sha)
            git_command(history, "update-ref", "refs/v2c/final", final_sha)
            findings = scan_secrets(history, include_git_history=True)
            if findings:
                raise SecretScanError(findings)
            # Private acceptance never belongs in the source history. If one is
            # supplied, fail closed; omitting it from code alone cannot scrub Git.
            for line in git_command(history, "rev-list", "--objects", "--all").splitlines():
                if " " in line:
                    name = repository.name + "/" + line.split(" ", 1)[1]
                    if any(name == p or name.startswith(p + "/") for p in private_paths):
                        raise DeliveryError("PRIVATE_ACCEPTANCE_IN_GIT_HISTORY")
            bundle = package / "git" / f"{repository.name}.bundle"
            bundle.parent.mkdir(exist_ok=True)
            git_command(
                history, "bundle", "create", str(bundle), "refs/v2c/source", "refs/v2c/final"
            )
            git_command(history, "bundle", "verify", str(bundle))
            provenance.append(
                {
                    "name": repository.name,
                    "source_revision": source_sha,
                    "final_revision": final_sha,
                    "final_tree_sha256": repository.sha256,
                    "source_tree_sha256": previous.sha256 if previous else repository.sha256,
                }
            )
        # The isolated runner uses repositories/<name>; the owner's package uses
        # code/<name>. One mapper owns every structured delivery-facing path.
        project_manifest = _portable_passport(
            project_manifest, package, {r.name for r in snapshot.repositories}
        )
        _write_json(package / "docs" / "system-map.json", project_manifest.get("map", {}))
        _write_json(
            package / "docs" / "environment.json",
            {
                "path_base": "delivery_root",
                "recipe": project_manifest.get("environment_recipe", {}),
                "commands": project_manifest.get("commands", {}),
                "unknown": not bool(project_manifest.get("environment_recipe")),
            },
        )
        _write_json(package / "docs" / "changes.json", project_manifest.get("changes", []))
        # Public verification summaries only. Hidden assertions/answers are never copied.
        public_verification = {
            key: verification[key]
            for key in (
                "status",
                "summary",
                "checks",
                "artifact_refs",
                "human_acceptance",
                "ai_acceptance",
            )
            if key in verification
        }
        public_verification["limitations"] = limitations
        _write_json(package / "docs" / "verification.json", public_verification)
        _skills(package, snapshot, project_manifest, packaged_files)
        findings = scan_secrets(package)
        if findings:
            raise SecretScanError(findings)
        manifest = {
            "version": 1,
            "status": "CANDIDATE",
            "project_id": snapshot.project_id,
            "snapshot_id": snapshot.snapshot_id,
            "repositories": provenance,
            "limitations": limitations,
            "files": [
                {
                    "path": p.relative_to(package).as_posix(),
                    "size": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    "executable": bool(p.stat().st_mode & 0o111),
                }
                for p in sorted(package.rglob("*"))
                if p.is_file()
            ],
        }
        _write_json(package / "manifest.json", manifest)
        archive = workspace / "candidate.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    output.write(path, path.relative_to(package).as_posix())
        artifact = None
        if store:
            body = archive.read_bytes()
            key = f"deliveries/{snapshot.project_id}/{hashlib.sha256(body).hexdigest()}.zip"
            artifact = store.put_bytes(key, body)
        # Candidate path appears only after complete ZIP+manifest and storage verification.
        archive.rename(destination)
        return DeliveryBundle(destination, manifest, artifact)

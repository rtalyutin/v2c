# Runner protocol v1

The control application never executes a project command locally. The runner
service lives on a disposable VM; its project containers have no control-plane
credentials or Docker socket. Provisioning and VM destruction use a separate
operator-supplied HTTPS gateway. No cloud vendor is assumed to implement this
gateway protocol directly.

The repository includes `v2c.timeweb_gateway`, which translates this contract
to the verified Timeweb API profile. Deployment/template instructions are in
[timeweb.md](timeweb.md); other clouds require their own gateway.

## Transport and authorization

Both clients require HTTPS, mutual TLS (configured CA/client certificate/key),
and an `Authorization: Bearer <secret>` header. Redirects and automatic HTTP
retries are disabled. Configure firewall restrictions outside the guest VM.
The command bearer secret does not grant authority to provision/destroy VMs;
the VM gateway has its own token and stays outside project containers.

For a dynamic runner IP, `MTLSConfig.server_name` supplies the expected DNS
SAN through SNI/Host while retaining CA and hostname verification. The IP
connection does not require public DNS or disabled TLS verification.

IDs match `[A-Za-z0-9_-]{1,128}`. Requests reject extra fields. A response body
must fit the configured byte limit. The exact schemas are `PrepareRequest`,
`AuthorizationRequest`, `CommandRequest`, `CommandResult` and `VMAllocation`
in `src/v2c/adapters/runner.py`; the supported test-service schema is
`PostgresService` in the same module.

## Runner endpoints

| Method and path | Input | Result |
| --- | --- | --- |
| `GET /health` | Authentication | Protocol/status, allocation ID and computed public runner profile hash |
| `POST /v1/workspaces` | Workspace/attempt IDs, generation, base64 ZIP, archive SHA256, input manifest | Prepared workspace |
| `PUT /v1/authorization` | Workspace ID, attempt ID, generation | Current authorization |
| `POST /v1/commands` | Command/workspace/attempt IDs, generation, argv, cwd, timeout | HTTP 202 and stored command state |
| `GET /v1/commands/{command_id}` | Command ID | Readback of command state |
| `POST /v1/commands/{command_id}/cancel` | Command ID | Cancellation/readback result |
| `GET /v1/workspaces/{workspace_id}/checkpoint` | Workspace ID | Bounded ZIP of regular project files |
| `DELETE /v1/workspaces/{workspace_id}` | Workspace ID | Workspace removed; `vm_destroyed: false` |

`cwd` is relative to the imported workspace. `argv` is an argument list, not
a shell script evaluated by the runner. Explicitly approved shell commands
can still invoke a shell inside the project container. Docker settings,
image digest and resource limits come from the operator profile, not a
project manifest.

`command_id` and its canonical request hash are journalled durably before
execution starts. An identical duplicate returns the stored result; the same
ID with different content conflicts. An older generation/attempt is rejected.
The journal is not a substitute for the controller's attempt fence.

Command states are `ACCEPTED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`,
`UNKNOWN`. Results include `exit_code`, bounded `output`, `output_truncated`,
`stop_confirmed` and `reason`. A process restart marks nonterminal journal
entries `UNKNOWN`; status readback inspects the corresponding container.
No entry, a timeout, or an HTTP error is proof that a command did not execute.
When the outcome cannot be established, destroy the VM through the provider
and confirm destruction before restoring the prior checkpoint elsewhere.
Terminal results require `stop_confirmed: true`; `SUCCEEDED` additionally
requires exit code zero.

Checkpointing requires an idle workspace and confirmed stopped commands.
Symlinks, special files and configured file/size overflows are rejected.
The ZIP contains project files only. The supported PostgreSQL service is
recreated from the controller's pinned migration/fixture recipe and verified
by its recovery checks. Checkpoint ZIPs are not database snapshots. Removing
a workspace is not proof of VM destruction.

## Synthetic PostgreSQL service

`input_manifest.environment_recipe.services` is a list with zero or one
strictly validated service. Currently the only supported shape is:

```json
{
  "name": "testdb",
  "kind": "postgres",
  "database": "project_test",
  "user": "project_test",
  "env_var": "DATABASE_URL"
}
```

Service names, database names and user names match
`[a-z][a-z0-9_]{0,31}`. Extra service fields and other service kinds are
rejected. The recipe must select `state_strategy: rebuild_from_fixtures`
and supply nonempty `restore` and `recovery_checks` command lists.

The runner operator must configure `postgres_image` pinned by SHA256 digest
and explicit `postgres_data_bytes`, `postgres_ready_seconds`, `postgres_uid`
and `postgres_gid`. The optional image defaults to absent; requesting a
service without an enabled operator profile returns `UNSUPPORTED`. The image
must run as the configured nonroot account and support `PGDATA=/pgdata`.
Images are preloaded by the operator; execution uses `--pull=never`.

The runner creates one workspace-owned internal Docker network and an
ephemeral PostgreSQL container without host port publication. The project
can reach only that internal service; external network access stays disabled.
The requested network policy remains `none`, meaning no external network.
Without a service the project container uses Docker network `none`.

A generated synthetic password and `DATABASE_URL` stay inside the runner's
runtime state and the corresponding test containers. Production database
credentials and control-plane PostgreSQL credentials are not used. CPU,
memory and PID allocations are split between project and test database;
their totals do not exceed the admitted profile. The disk budget must cover
the dedicated workspace filesystem plus PostgreSQL's bounded tmpfs data.

Every fresh VM receives a fresh database. The controller restores schema and
synthetic fixtures before continuing and runs recovery checks. A saved ZIP
alone does not establish that database recovery succeeded.

## Runner configuration sources

The service accepts `--config /operator/runner.json`. Without that argument,
`RunnerSettings.from_env()` first reads the JSON file named by
`V2C_RUNNER_SETTINGS_FILE`; otherwise it maps model fields from
`V2C_RUNNER_<FIELD>` environment variables, for example
`V2C_RUNNER_DATA_DIR` and `V2C_RUNNER_POSTGRES_IMAGE`.
`--tls-cert`, `--tls-key` and `--client-ca` remain mandatory CLI arguments in
all configuration modes. Runner settings belong to the disposable VM and
must not be confused with control-plane connection settings.

## Operator VM gateway

| Method and path | Input | Result |
| --- | --- | --- |
| `POST /v1/vms` | `allocation_id`, `purpose` | `VMAllocation` |
| `GET /v1/vms/{vm_id}` | VM ID | Current allocation state |
| `DELETE /v1/vms/{vm_id}` | VM ID | Current destruction state |

An allocation has `vm_id`, `runner_endpoint` (HTTPS), and state `CREATING`,
`READY`, `DESTROYING`, `DESTROYED` or `UNKNOWN`. Optional `reason` is a bounded
uppercase diagnostic code, never a cloud response body. The gateway must persist the
`allocation_id` before provisioning and return the same allocation for a
retry. It must not return `READY` before the configured runner is reachable
with the intended identity, image and limits. A deletion response merely
accepting work must report `DESTROYING`; only confirmed destruction reports
`DESTROYED`.

The gateway must allocate fresh VMs, enforce the host/network/disk policy,
retain operator-only provisioning credentials, and provide authoritative
status readback. These remain deployment integration requirements.
`v2c.timeweb_gateway` supplies the Timeweb implementation, durable ownership
journal and reviewed external firewall profile. Its runtime behavior still
requires acceptance in the target account; local test ports do not establish
actual provisioning, deletion or network isolation.

## Validation scope

Unit tests exercise the protocol client and runner logic using controlled
HTTP/Docker backends. They do not prove actual VM creation, Docker isolation,
cloud firewall policy, TLS deployment or recovery of a real test database.
Those require explicit integration acceptance on the target environment.

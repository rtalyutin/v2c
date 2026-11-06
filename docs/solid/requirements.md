# Матрица 47 требований

**Публикация и последующий CI:** [PUBLICATION.md](PUBLICATION.md) и [PR #2](https://github.com/rtalyutin/v2c/pull/2). Ниже — локальный checkpoint до публикации.

Кандидат исходников и тестов: `be5af35ce8915fb7164cd6efa55617ee26acebc8`.
[Отчёт](verification.md), [владельцы](architecture.md), [порты](ports.md).

**LOCAL PASS** — указанная автоматическая проверка в доступной среде.
**BLOCKED** — обязательная часть доказательства недоступна. Это не PASS.
Ни одна строка LOCAL PASS не означает независимую человеческую приёмку или live cloud.
Полное ТЗ остаётся **BLOCKED** до полного технического профиля и T14.

Сокращения тестов: S — [test_solid_contracts](../../tests/test_solid_contracts.py);
P — [shared port behaviors](../../tests/test_solid_port_behaviors.py);
D — [developer checks](../../tests/test_developer_checks.py);
W — [workflow contracts](../../tests/test_workflow_contracts.py);
J — [journal versions](../../tests/test_journal_versions.py);
PG — [PostgreSQL](../../tests/test_postgres.py);
API — [API contracts](../../tests/test_api_contracts.py);
PL — [pipeline](../../tests/test_pipeline.py);
A — [adapters](../../tests/test_adapters.py).
Эти ссылки — исполняемая область доказательства, не обещание полного покрытия.

| ID | Изменённый код / доказательство | Фактический статус |
|---|---|---|
| G01 | S/O/L/I/D ниже имеют собственные изменения и проверки; T07/T14 и полный профиль остаются открыты | BLOCKED |
| G02 | architecture.md, ports.md, основной architecture/change-guide/workflow обновлены | LOCAL PASS: links + карта владельцев |
| G03 | Проверяется весь прежний test suite; runner/provider/схема/EAV/панель сохранены как стабильные владельцы, без необоснованного переноса | BLOCKED: локальные тесты пройдены, полный профиль нужен |
| C01 | Процессы и production DockerBackend не объединялись; runner client остаётся внешним | LOCAL PASS: runner/PL/P; live isolation не заявлена |
| C02 | RunStore/LeaseStore/PlanStore используют прежний общий ControlDatabase._write/current | BLOCKED: Engine/W проходят, PG rollback/races не выполнены здесь |
| C03 | OperationJournal и CommandStore сохраняют identity/canonical hash/readback | LOCAL PASS: W, PL lost response, J, P |
| C04 | Правила retry/approval/READY не менялись; EvidenceStore и PlanStore | LOCAL PASS: Engine, W, PL, acceptance |
| C05 | Schema/journal/checkpoint форматы и golden bytes не менялись; миграция не нужна | LOCAL PASS: J/W/API, SQLite upgrade/check; PG schema часть BLOCKED |
| C06 | Старые импортные пути/CLI/HTTP DTO сохранены, delta только в недопустимом вводе/lifecycle | LOCAL PASS: API/configuration/runner/deployment и S |
| S01 | build_components/build_executor в composition.py; production build_pipeline внедряет StageComponents | LOCAL PASS: S independent model/extension, PL |
| S02 | StageHandlers отдельно от StageExecutor; cleanup/admission/finish в executor | LOCAL PASS: S/PL |
| S03 | RunStore, LeaseStore, PlanStore; EvidenceStore/HandoffSealer сохраняют handoff | BLOCKED: локальный Engine/W/PL PASS, PG atomic proof не выполнен |
| S04 | ExecutionOperations не принимает ensure_model_ready/model; StageSpec задаёт admission | LOCAL PASS: S missing model + released policies, PL |
| S05 | application/imports, decisions, handoff не требуют Request/app.state; API owner checks остаются | LOCAL PASS: API/acceptance и import tests |
| S06 | Таблица каждого изменённого владельца и объяснение совместимых фасадов | LOCAL PASS: architecture.md + links |
| O01 | Явные StageSpec и неизменяемый StageRegistry; прежний порядок add_step/logical_key | LOCAL PASS: S extension/policies, PL/W |
| O02 | Executor не ветвится по именам стадий; duplicate/capability/unknown контролируются | LOCAL PASS: S registry/unknown/missing model |
| O03 | Все семь прежних handler sequences и admission policies сохранены | LOCAL PASS: S released policies, PL полный сценарий |
| O04 | local_probe добавляется только handler/StageSpec; модель и VM отсутствуют | LOCAL PASS: S extension |
| O05 | ModelPort/RunnerPort/VMProviderPort/ArtifactStore; class-name/provider switches в executor отсутствуют | LOCAL PASS: S independent model, P, D |
| O06 | Обычная явная сборка; нет scanner, DI container или глобального registry | LOCAL PASS: StageRegistry/композиция и S |
| L01 | ModelDescription/ModelReadiness/PreparedModelRequest + полный ModelPort | LOCAL PASS: S shared model + strict mypy |
| L02 | ModelOperations не читает profile/secret_ref/request_body/is_test; adapter владеет env/serialization | LOCAL PASS: S independent slotted model, PL presets |
| L03 | ports.md задаёт ошибки, delivery uncertainty, identity, readback, cancel и resource ownership | LOCAL PASS: P/A/runner/gateway/J; live services не квалифицированы |
| L04 | Общие Model/P тесты выполняются на HTTP/local/S3 и независимых doubles; UNKNOWN/restart дополняют прежние A/PL/runner/gateway/J | LOCAL PASS в детерминированной среде; не cloud/S3 production proof |
| L05 | tests/solid_model.py не наследует OpenAI и не имеет прежних скрытых атрибутов | LOCAL PASS: strict mypy + S real consumer + D negative GenerateOnly |
| L06 | Нейтральные DTO re-export прежними модулями; профиль run не переопределяется | LOCAL PASS: API/W/PL golden и binding |
| I01 | Consumer-owned control Protocol; ControlPlane только в composition/facade | LOCAL PASS: strict mypy, S minimal packaging, D |
| I02 | Packaging получает work_root/instruction_max_bytes; source — work_root; VM/model — связанные лимиты | LOCAL PASS: S packaging без Settings и несвязанных методов |
| I03 | Write-порты предоставляют полные сценарии, не Session и не независимые commits | BLOCKED: структура + локальный rollback PASS, PG нужен |
| I04 | Необязательная модель допускается для local_probe/package; capability даёт явный отказ | LOCAL PASS: S registry/extension/missing model |
| I05 | Runner/VM/storage остаются связными портами, общий CleanupPort отделён по потребителю | LOCAL PASS: P, strict mypy |
| D01 | contracts/model, runner, storage, source, proposals, control не импортируют infrastructure даже в TYPE_CHECKING | LOCAL PASS: architecture + D negative imports |
| D02 | workflow только stdlib/contracts; orchestration/application без Engine/ORM/httpx/boto3 | LOCAL PASS: architecture + D positive/negative |
| D03 | config/composition строят инфраструктуру; lifecycle владельцев документирован в ports.md | LOCAL PASS: P borrowed clients + PL cleanup |
| D04 | ORM/Session остаются persistence и legacy Engine bootstrap; сценарии атомарные, внешних вызовов в транзакции нет | LOCAL PASS: architecture + Engine/W; конкурентная часть относится C02 |
| D05 | AST проверяет runtime/type imports и циклы; conformance в scripts | LOCAL PASS: D 14 отрицательных/положительных import cases + conformance |
| D06 | Старые направленные re-export и entrypoints без обратных imports/cycles | LOCAL PASS: architecture, API/configuration/deployment |
| B01 | Общие argv/cwd validators; Acceptance и RecipeCommand; 422 до supply_input | LOCAL PASS: S invalid cwd/argv/state unchanged, API |
| B02 | ValidationError → INVALID_STAGE_INPUT; безопасный NEEDS_INPUT либо RUNNING + fenced EXECUTION_PENDING | LOCAL PASS: S legacy input/correction/unknown effect/slot |
| B03 | run_blocking и worker supervision; source/store/archive/package/context вне event loop | LOCAL PASS: S slow store heartbeat, repeated cancel, worker cancellation, stale generation |
| B04 | AdapterError/RunnerError и нормализованные StorageError; safe error_category + operation identities | LOCAL PASS: A/P/diagnostics, S pending event |
| B05 | Golden fixtures неизменны; journals UNKNOWN и pending allocation/delete/firewall читаются после рестарта | LOCAL PASS: API/W/J/gateway/runner/PL |
| B06 | Отдельные commits neutral values, transactional owners, model contract, composition и behavior fixes | LOCAL PASS: git history; история не переписана в один непрозрачный diff |
| A01 | Ветка с исходниками/тестами; architecture, workflow, guide, ports, facade/migration решения | LOCAL PASS: локальная ветка и docs; remote delivery не выполнена |
| A02 | Все 47 ID имеют код/проверку/результат или явный BLOCKED | LOCAL PASS: эта матрица |
| A03 | verification.md, raw evidence, candidate SHA, migration delta и known issues | BLOCKED: локальный отчёт подготовлен, CI/независимая проверка отсутствуют |

## T01–T14

| Тест | Доказательство / граница |
|---|---|
| T01 | S independent real consumer; strict tests/solid_model.py; D GenerateOnly отвергается |
| T02 | S shared ModelPort; P runner/VM/storage; A/runner/gateway/J uncertainty/restart |
| T03 | S registered_extension_needs_no_model_or_vm |
| T04 | S registry invalid composition, missing model, persisted unknown stage |
| T05 | S packaging minimal control/source/work_root; strict ports/conformance |
| T06 | D forbidden runtime and TYPE_CHECKING imports, positive counterparts, cycles |
| T07 | BLOCKED: два PG tests существуют, здесь пропущены из-за отсутствия PostgreSQL |
| T08 | API/W/J golden fixtures, DTO hashes/null, no schema migration |
| T09 | PL lost response; runner/gateway/J restart and pending intents |
| T10 | Engine/W/runner/PL fences/cancel; S late blocking producer and supervisor cancel |
| T11 | S 422 unchanged state; legacy invalid payload/corrected input |
| T12 | S real worker heartbeat through 1.2s storage operation with 1s lease; cancellation/join/stale fence |
| T13 | Engine/W/PL/acceptance exact revision, final digest and READY |
| T14 | BLOCKED / NOT EXECUTED: нужен другой Python-разработчик и задача/скрытые проверки после фиксации SHA |

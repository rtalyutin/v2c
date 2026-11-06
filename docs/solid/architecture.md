# Владельцы SOLID-рефакторинга

База: `0abb85c0a343b84248edfffaa8d8b844804bccb9`.
[ТЗ](specification.txt), [статус](TASK_STATE.md), [матрица](requirements.md),
[контракты портов](ports.md). Это карта реализации; независимая приёмка ещё не выполнена.

## Ответственность и зависимости

| Класс / модуль | Причина изменения, публичный контракт | Допустимые зависимости |
|---|---|---|
| [config.py](../../src/v2c/config.py), [composition.py](../../src/v2c/composition.py) | Сборка приложения, конкретные клиенты, настройки, регистрация StageSpec; build_components/build_executor/build_pipeline/build_git_fetcher | Контракты, сервисы, инфраструктура |
| [Engine](../../src/v2c/engine.py) | Совместимый API control plane и сборка stores; прежние сигнатуры публичных операций | Контракты, workflow, persistence, ORM только в dev bootstrap/legacy helpers |
| [WorkflowRows](../../src/v2c/persistence/workflow_rows.py) | Общие DB/clock, lock/current/event helpers без самостоятельной транзакции внутри сценария | ControlDatabase, ORM, rules |
| [RunStore](../../src/v2c/persistence/runs.py) | Создание/чтение runs и шагов, вход, неизменяемое binding модели | Та же ControlDatabase, WorkflowRows, rules |
| [LeaseStore](../../src/v2c/persistence/leases.py) | Claim, heartbeat, finish, recovery, cancel, reconciliation и note_pending | Та же ControlDatabase, WorkflowRows, rules |
| [PlanStore](../../src/v2c/persistence/plans.py) | Публикация и решение по точной revision/snapshot | Та же ControlDatabase, WorkflowRows, rules |
| [EvidenceStore](../../src/v2c/persistence/evidence.py), MetadataStore, CommandStore, UsageStore | Прежние владельцы evidence/READY, метаданных, команд и usage; поведение не переносилось | Persistence, ORM, нейтральные значения |
| [Pipeline](../../src/v2c/pipeline.py) | Фасад прежних импортов, конструктора, приватных hooks и execute; production получает StageComponents | Composition root, нейтральные порты, StageHandlers |
| [StageComponents](../../src/v2c/orchestration/components.py) | Неизменяемый набор подготовленных сотрудников; не создаёт клиентов | Порты и связные прикладные сервисы |
| [StageRegistry](../../src/v2c/orchestration/registry.py) | Локальная неизменяемая регистрация kind/handler/requirements; admit | StageSpec, DomainError |
| [StageExecutor](../../src/v2c/orchestration/executor.py) | Общий attempt lifecycle, admission, классификация ошибок и безопасное завершение | AttemptControl, OperationJournal, CleanupPort, StageRegistry; никаких имён этапов |
| [StageHandlers](../../src/v2c/orchestration/stages.py) | Последовательности семи штатных этапов; локальные методы этапов сохраняют прежние logical_key и порядок создания шагов | StageControl и подготовленные сервисы; никакой сборки инфраструктуры |
| [OperationJournal](../../src/v2c/orchestration/journal.py) | Guard и write-before-dispatch identity; prepare/observe | JournalControl, нейтральные контракты |
| [ModelOperations](../../src/v2c/orchestration/model.py) | Выбор контекста, binding, durable model dispatch/usage/response validation | ModelControl, ModelPort, OperationJournal, PipelineLimits |
| [ExecutionOperations](../../src/v2c/orchestration/execution.py) | VM lifecycle, runner commands и reconciliation | ExecutionControl, RunnerPort, VMProviderPort, execution limits; без модели |
| [SourceArtifacts](../../src/v2c/orchestration/sources.py) | Источник/checkpoint и provenance | SourceControl, ArtifactStore, RunnerPort, journal, work_root и связанные лимиты |
| [PackagingOperations / PackagingSources](../../src/v2c/orchestration/packaging.py) | Candidate bundle и ограниченное чтение инструкций | PackagingControl, ArtifactStore, PackagingSources, work_root и конкретный instruction_max_bytes; без Settings и модельных операций |
| [HandoffSealer](../../src/v2c/orchestration/handoff.py) | Запечатывание принятого digest с актуальными receipts | HandoffControl, ArtifactStore |
| [application](../../src/v2c/application) | import_snapshot, decide_plan, supply_acceptance, record_human_handoff без Request/app.state | Consumer-owned порты и валидированные входы; GitFetcher внедрён |
| [worker](../../src/v2c/worker.py) | Супервизия выполнения/heartbeat/отмены | WorkerControl, PipelineRuntime; concrete wiring только в main |
| [OpenAICompatibleModel](../../src/v2c/adapters/model.py) | Провайдерский JSON, готовность/секреты, описание профиля и измерение запроса | ModelPort DTO, httpx/jsonschema; заимствованный HTTP-клиент не закрывается |
| [RunnerClient / HTTPVMProvider](../../src/v2c/adapters/runner.py) | Runner/VM HTTP transport, нормализация ошибок и идентичности | Нейтральные runner DTO, HTTP/TLS; поведение сохранено, is_test теперь объявлен в порту |
| [LocalArtifactStore / S3ArtifactStore](../../src/v2c/storage.py) | Immutable publication/readback; локальные OS errors нормализованы | ArtifactStore/ArtifactRef, filesystem или заимствованный S3-клиент |
| [contracts](../../src/v2c/contracts) | Model/runner/storage/source/proposal/operator/workflow DTO и consumer-owned Protocol | Стандартная библиотека, Pydantic; без adapters/config/ORM даже в TYPE_CHECKING |
| [diagnostics](../../src/v2c/diagnostics.py) | Безопасная корреляция и стабильная категория ошибки | Нейтральные классы ошибок; без message, locals, raw response или source |
| [check_architecture / check_conformance](../../scripts) | Проверяемые направления импортов, циклы и structural subtyping | В проверочном слое допустимы импорты обеих сторон |

Семь handlers остаются одним связным координатором последовательностей. Решения об
admission находятся в регистрации, упаковка/модель/VM имеют отдельных владельцев.
Общий executor не меняется при добавлении этапа. Деление handlers на семь файлов
само по себе не улучшает эту границу.

## Атомарность

Все stores получают **один экземпляр ControlDatabase**, одну DB и clock. Guard
generation/lease, связанные ORM-записи и event остаются внутри прежнего `_write`.
Facade делегирует целую операцию, а не выполняет несколько независимых commits.
HTTP/model/VM вызовы не перенесены в транзакции. PostgreSQL rollback/race сценарии
остаются обязательными; SQLite не заменяет их доказательство.

## Совместимость

Канонические DTO теперь в contracts/model.py, runner.py, storage.py, source.py и
proposals.py. Старые adapters/model.py, adapters/runner.py, storage.py, intake.py,
orchestration/schemas.py и persistence/records.py экспортируют прежние символы.
Pipeline, Engine, runner_service и timeweb_gateway сохраняют entrypoints.
Conformance перенесён из contracts/conformance.py в scripts/check_conformance.py:
это проверочный модуль, а не runtime API.

Удаление фасадов — отдельная задача после перевода известных потребителей,
bootstrap/systemd/Compose и rollback-образов. Наличие старого импорта в установленной
VM не проверяется поиском только по этому checkout.

Схема control DB, EAV, journals и checkpoints не меняется; миграция не нужна.
Golden fixtures не перегенерированы. Намеренные изменения недопустимого входа и
отмены перечислены в [отчёте](verification.md).

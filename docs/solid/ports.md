# Контракты интеграций

Контракт описывает эффект и неопределённость, а не только сигнатуры.
Поведенческие проверки: [shared ports](../../tests/test_solid_port_behaviors.py),
[model/consumer/lifecycle](../../tests/test_solid_contracts.py), существующие
[adapter](../../tests/test_adapters.py), [runner](../../tests/test_runner.py),
[gateway](../../tests/test_timeweb_gateway.py) и [journal](../../tests/test_journal_versions.py) сценарии.

| Порт | Предусловие / результат | Повтор, UNKNOWN, отмена, ресурсы |
|---|---|---|
| ModelPort | describe возвращает стабильные version/manifest/max_input_bytes; manifest безопасен и version совпадает. readiness — локальный admission. prepare — чистые детерминированные полные wire bytes без credentials, включая task/schema overhead. generate получает тот же ModelRequest и возвращает ModelResult/usage либо AdapterError | Один dispatch без скрытого retry. ModelPort не обещает provider readback; потерянный ответ сохраняет usage/delivery uncertainty, оркестратор не повторяет его вслепую. Подготовка не обращается к сети. Заимствованный HTTP client остаётся открыт; собственный клиент ограничен одним вызовом |
| RunnerPort | Идентификаторы, archive checksum, workspace, attempt/generation валидны; подготовка точного workspace и authorize предшествуют command. Результат содержит точные command_id и content_hash | Повтор того же ID/hash возвращает сохранённое состояние; другой hash — CONFLICT. Новая работа со старой generation — STALE_GENERATION. После lost response только status прежнего ID. UNKNOWN не равен отсутствию эффекта. cancel считается остановкой только при stop_confirmed. Checkpoint не даёт разрешения запуска. Borrowed HTTP client не закрывается |
| VMProviderPort | Устойчивый allocation_id/purpose сохранён до create; caller владеет journal и лимитами. create/status/destroy возвращают VMAllocation, endpoint проверяется до runner dispatch | Повтор одного ID/purpose не создаёт второй ресурс; другой purpose — CONFLICT. UNKNOWN/CREATING/delete acceptance не доказывают остановку; нужен readback. DESTROYED — подтверждённая остановка, повтор create прежнего ID не создаёт новую VM. Gateway проверяет ownership до удаления. Borrowed HTTP client не закрывается |
| ArtifactStore | Безопасный относительный object key; bytes неизменяемы. put возвращает ArtifactRef только после readback/checksum. get/exists не исполняют данные | Повтор key/bytes идемпотентен. Другие bytes не перезаписывают объект; конфликт или checksum error. Ошибка read/upload — StorageError с безопасным кодом. После неопределённой загрузки разрешён повтор того же immutable key/bytes с readback, а не замена. Объекты не удаляются при отмене producer; незаписанный в DB объект не становится delivery. S3 body закрывается, borrowed client остаётся открыт |

Model adapter владеет secret resolution и провайдерской сериализацией.
ModelOperations использует только четыре операции порта; не читает profile,
secret_ref, request_body или is_test. test_double — явное поле ModelDescription;
runner is_test объявлен свойством RunnerPort и служит только маркировке evidence,
не разрешению production переходов.

Ошибки модели: AUTH, INVALID_CONFIG, UNSUPPORTED_CAPABILITY, RATE_LIMIT,
TRANSPORT_OR_TIMEOUT, PROVIDER_FAILURE, INVALID_RESPONSE, CANCELLED.
AdapterError отдельно сохраняет retryable, delivery_unknown, usage_unknown и
provider_request_id. RunnerError хранит code/delivery_unknown. StorageError
возвращает коды, например OBJECT_READ_FAILED, OBJECT_UPLOAD_FAILED,
OBJECT_CHECKSUM_MISMATCH, IMMUTABLE_OBJECT_CONFLICT, INVALID_OBJECT_KEY.
В логе пишутся класс и безопасная категория (включая RUNNER_FAILURE,
STORAGE_FAILURE), operation IDs и позиции кадров, но не message. Конкретная
причина/наблюдение сохраняется в fenced durable command/event.

## Порты потребителей

[control.py](../../src/v2c/contracts/control.py) задаёт JournalControl,
ModelControl, ExecutionControl, SourceControl, PackagingControl, HandoffControl,
StageControl, AttemptControl, WorkerControl, ImportControl, DecisionControl,
AcceptanceControl и HumanHandoffControl. Каждая write-операция — полный атомарный
сценарий. Никаких ORM Session у потребителя. ControlPlane — только aggregate
composition root; новые сервисы не получают его вместо своего порта.

PackagingSources требует только required_result для inspect/verify/package,
source, artifact_bytes и completed. PackagingControl — record_artifact и
verifications. Настройки упаковки — путь и лимит инструкций, не Settings.
CleanupPort — allocation/destroy/cancel_run для общего lifecycle. PipelineRuntime —
execute/cancel_run/reconcile для worker. ImportSettings — только work_root и
max_upload_bytes; GitFetcher внедряется отдельно.

TypedDict записей не переписывает persisted JSON. Proposal/HandoffInput DTO
декодируют содержимое до эффекта; RecipeCommand и AcceptanceCheck используют
общие argv/cwd validators. Границы DB/провайдерского JSON и расширяемые metadata
сохраняют JSON-форму и существующие hashes. Не добавлять defaults или новый
discriminator в уже подписанный request ради типизации.

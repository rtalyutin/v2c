# Где менять поведение

Начните с [карты владельцев](architecture.md), воспроизводимой dev-среды и команд из [CONTRIBUTING](../CONTRIBUTING.md). Ниже указаны точки изменения и проверки, позволяющие ограничить область чтения. Файлы контрактов и тесты — источники точных полей/статусов; этот документ не задаёт их вторую независимую версию.

## Поле operator API

Пример следующей небольшой задачи: добавить необязательное диагностическое поле ответа run. Сначала определить значение отсутствия: поле всегда присутствует со значением `null` или действительно может отсутствовать. Это отдельное изменение wire-контракта, а не побочный эффект добавленной ORM-колонки.

1. Изменить соответствующий output DTO в [contracts/operator.py](../src/v2c/contracts/operator.py) и явно сформировать значение в [api.py](../src/v2c/api.py) либо прикладном сценарии. Для хранимого значения также нужны record type и миграция; для вычисляемого поля колонка не обязательна. Не обходить `public()` и owner check.
2. Проверить фактический HTTP-ответ по схеме, `null`, отсутствие доступа чужого владельца и отсутствие внутренних полей в [test_api_contracts.py](../tests/test_api_contracts.py) и [test_api.py](../tests/test_api.py). Зафиксировать намеренную delta к [baseline wire fixtures](../tests/fixtures/operator-wire/README.md); не перезаписывать историческую baseline как будто новый контракт существовал раньше.
3. Выполнить `python scripts/export_openapi.py`; просмотреть diff [operator-openapi.json](contracts/operator-openapi.json). CI вызывает `--check`. Этот JSON не редактируется вручную.
4. Если поле отображается оператору, изменить renderer панели и браузерный сценарий. Сохранить существующие URL, статусы и поля, если задача явно не меняет их.

Для новой ошибки проверить фактический HTTP status и `detail`, затем объявить error response на route. Наличие модели ошибки само по себе не документирует конкретный путь отказа.

## Ошибка runner или поведение внешней команды

Пример: добавить контролируемую ошибку недопустимого запроса runner. Её источник находится в [runner/service.py](../src/v2c/runner/service.py) или [runner/docker.py](../src/v2c/runner/docker.py), HTTP mapping — в [runner/api.py](../src/v2c/runner/api.py), клиентские DTO/ошибки — в [adapters/runner.py](../src/v2c/adapters/runner.py). Для cloud lifecycle соответствующие владельцы находятся в [providers/timeweb](../src/v2c/providers/timeweb).

Указать в [runner-protocol.md](runner-protocol.md), произошёл ли эффект до ошибки и как его узнать. Timeout транспорта не превращать в доказанный `FAILED`. Для изменения retry-классификации проверить [workflow/states.py](../src/v2c/workflow/states.py), [orchestration/execution.py](../src/v2c/orchestration/execution.py) и [journal.py](../src/v2c/orchestration/journal.py). Это разные уровни: ошибка команды, неопределённость внешнего эффекта и право на новую attempt.

Проверки: [test_runner.py](../tests/test_runner.py), [test_adapters.py](../tests/test_adapters.py), [test_pipeline.py](../tests/test_pipeline.py). Проверить повтор с тем же ID, конфликт другого запроса под тем же ID, потерянный ответ, readback и устаревшую generation; для изменённого ветвления нужны как разрешённый, так и запрещённый случай.

## Этап Pipeline, retry или завершение run

Для изменения существующего этапа откройте его `_stage_*` в [pipeline.py](../src/v2c/pipeline.py). Модельный запрос делегируется [ModelOperations](../src/v2c/orchestration/model.py), VM/команды — [ExecutionOperations](../src/v2c/orchestration/execution.py), checkpoint/артефакт — [SourceArtifacts](../src/v2c/orchestration/sources.py). Не переносите transport, ZIP и lifecycle VM обратно в метод этапа.

Новый вид этапа требует явной записи в `Pipeline.stages`, типа результата в [contracts/workflow.py](../src/v2c/contracts/workflow.py) и места создания шага в [Engine](../src/v2c/engine.py). Если этап меняет план/recipe, проверить схемы [orchestration/schemas.py](../src/v2c/orchestration/schemas.py), frozen check IDs и approval revision. Одного добавления обработчика недостаточно: шаг должен попасть в долговечную последовательность. Неподдерживаемый вид продолжает давать контролируемую ошибку.

Retry/completion policy находится в [workflow/states.py](../src/v2c/workflow/states.py); транзакционное применение — в Engine. Изменение terminality требует проверки соответствующего домена, а не общего поиска и замены строки во всех state sets. Проверки fencing находятся в [ControlDatabase.current](../src/v2c/persistence/unit_of_work.py), проверки `READY` — в [EvidenceStore](../src/v2c/persistence/evidence.py) и [HandoffSealer](../src/v2c/orchestration/handoff.py). Guards и связанные записи должны оставаться в одной транзакции.

Проверки: [test_workflow_contracts.py](../tests/test_workflow_contracts.py), [test_engine.py](../tests/test_engine.py), [test_pipeline.py](../tests/test_pipeline.py), [test_acceptance.py](../tests/test_acceptance.py), а для гонок и locks — [test_postgres.py](../tests/test_postgres.py). Стадии тестируются через порты без реального облака; это не замена PostgreSQL или live VM проверкам затронутой границы.

## Лимит и настройка

Пример: изменить допустимый объём модельного контекста. Начать с `Settings`, `from_env` и сборки адаптера в [config.py](../src/v2c/config.py), затем проверить [PipelineLimits](../src/v2c/orchestration/common.py) и упаковку контекста в [orchestration/model.py](../src/v2c/orchestration/model.py). Для ресурсов исполнения проверить [ExecutionLimits](../src/v2c/contracts/workflow.py), runner request и проверку runtime в [runner/service.py](../src/v2c/runner/service.py).

Различайте конфигурацию процесса, зафиксированные limits run и лимит конкретного запроса. Не меняйте intent hash старого run и не подменяйте его limits значениями новых defaults. Проверяйте положительную валидацию, граничное превышение, действующий model preset и поведение повторного запроса. Тесты: [test_configuration.py](../tests/test_configuration.py), [test_adapters.py](../tests/test_adapters.py), [test_pipeline.py](../tests/test_pipeline.py). Значение timeout не является SLA.

## Миграция control DB или локального журнала

Для новой хранимой колонки изменить [models.py](../src/v2c/models.py), создать новую ревизию в [migrations/versions](../migrations/versions), обновить [persistence/records.py](../src/v2c/persistence/records.py) и владельца запросов. Не переписывать `0001_initial.py` для уже развёрнутой базы. EAV проекта обслуживает [MetadataStore](../src/v2c/persistence/metadata.py); задача на поле проекта не разрешает замену всей модели метаданных.

Проверить upgrade на отдельной базе с предыдущей схемой/данными, сохранение нужных значений, `alembic check` и PostgreSQL-интеграцию. `Engine.init_schema()` предназначен для disposable dev/test и не заменяет production migration. Не исправлять fencing или workflow ручным UPDATE базы.

Runner/gateway journals не управляются Alembic. У них собственный `PRAGMA user_version` в [runner/journal.py](../src/v2c/runner/journal.py) и [providers/timeweb/journal.py](../src/v2c/providers/timeweb/journal.py). Текущая версия — 1; распознаются пустой новый журнал и точная известная схема legacy v0. Нераспознанная схема/версия останавливает запуск без пересоздания. При adoption gateway v0 создаётся проверенная SQLite backup; runner при старте переводит незавершённые команды в `UNKNOWN` для readback.

При следующей версии задать явный переход, совместимость rollback и сохранение неизвестных эффектов. Использовать SQLite backup API, учитывающий WAL, а не копирование одного файла работающей базы. Дополнить [test_journal_versions.py](../tests/test_journal_versions.py): старые записи, terminal/UNKNOWN, pending delete/firewall, неподдерживаемая версия, повреждение и повторный запуск.

## Панель и диагностика

Изменение вызова API — [api-client.js](../src/v2c/web/api-client.js); выбора проекта/run — [state.js](../src/v2c/web/state.js); обновлений — [polling.js](../src/v2c/web/polling.js); отображения — [render.js](../src/v2c/web/render.js) и [panel.css](../src/v2c/web/panel.css). [panel.js](../src/v2c/web/panel.js) связывает эти части. Новому статическому файлу нужна запись в allowlist route `/web/{filename}` в API и проверка включения в package.

В [браузерных тестах](../tests/browser/README.md) проверить logout, смену выбранного проекта/run при задержанном ответе, прекращение polling и безопасное отображение текста. Возможности кнопок не заменяют серверные guards. Для нового диагностического контекста изменить разрешённые поля [diagnostics.py](../src/v2c/diagnostics.py) и [test_diagnostics.py](../tests/test_diagnostics.py); не логировать exception message, request body, токены или исходники проекта.

## Перед передачей изменения

В diff должны быть видны причина изменения, затронутый контракт и проверка его поведения. Запустить профиль `check` из CONTRIBUTING, соответствующий границе изменения. Для протокола/порядка эффектов обновить последовательность в документации вместе с contract delta; для текста достаточно проверки ссылок `python scripts/check_docs.py`. Указывать реальные PASS/FAIL/SKIP и оставшиеся live-проверки. Успех автотестов не закрывает независимую человеческую приёмку M10.

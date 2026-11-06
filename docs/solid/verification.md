# Фактическая проверка SOLID-кандидата

Дата: 2026-11-06. База: `0abb85c0a343b84248edfffaa8d8b844804bccb9`.
Кандидат кода/тестов: `be5af35ce8915fb7164cd6efa55617ee26acebc8`.
Ветка: `codex/solid-contracts-20261105`. Новая удалённая ветка/PR не создавались.
Документация и файлы evidence поставляются отдельным последующим commit;
они не меняют исходники кандидата. Полная приёмка **BLOCKED**.

## Проверки

Финальный прогон fast profile выполнен на исходниках указанного кандидата.
Точные команды и результаты сохранены в [evidence](evidence).

| Проверка | Результат |
|---|---|
| `python scripts/check.py` | PASS: 308 passed, 3 skipped, 1 warning; 75.46s для pytest |
| Ruff check/format | PASS, 122 Python files |
| Strict mypy | PASS, 73 source files |
| Architecture imports/cycles | PASS, включая TYPE_CHECKING и отрицательные контроли |
| Docs / OpenAPI | PASS; дополнительные ссылки итоговой документации проверены отдельно |
| SQLite Alembic upgrade/check | PASS, no new upgrade operations |
| PostgreSQL / Compose | SKIP: 2 PG cases + 1 Compose case; полный профиль BLOCKED |
| Wheel build/import/assets | PASS, установленный wheel и точные bytes всех web assets |
| Dependency locks / web formatting | PASS |
| Browser | BLOCKED до выполнения сценариев, host socket permission |
| Independent review / T14 | NOT EXECUTED |

- `python scripts/lock.py --check`: unchanged.
- `pip wheel --no-deps --no-build-isolation .`: wheel собран; build-only
  setuptools установлен в локальное окружение по существующему build-system.
  Runtime/developer locks и manifest зависимостей не менялись.
- `npm run check:web`: PASS.
- `V2C_BROWSER_EXECUTABLE=/usr/bin/chromium npm run test:ui`: BLOCKED.
  Chromium завершился до сценариев: `socket() failed: Operation not permitted`.
  8 browser cases не выполнены; это не PASS и не доказанный UI regression.
- `python scripts/check.py --integration --web`: BLOCKED без disposable
  V2C_TEST_POSTGRES_URL и Docker Compose. PostgreSQL rollback/races,
  PostgreSQL migrations и Docker image build здесь не проверены.
- Независимое ревью и T14 человеком: NOT EXECUTED. Авторские тесты, mypy и
  subprocess-проверки не являются независимым человеческим verifier.

Существующий CI содержит PostgreSQL 16, Compose, browser и docker build. Его
результат на старом PR не переносится на этот SHA. Для завершения технического
профиля нужен новый прогон на кандидатной ветке в среде с этими возможностями.

## Намеренная delta и миграция

1. Acceptance с небезопасным cwd/argv/allowed_new_files получает 422 до публикации
   входа. Успешный payload и присвоение acceptance_N IDs сохранены.
2. Старый недопустимый вход/чистая ValidationError дают INVALID_STAGE_INPUT.
   Без незавершённых commands — NEEDS_INPUT с освобождением slot; с UNKNOWN
   эффектом — RUNNING и fenced EXECUTION_PENDING, без нового dispatch/attempt.
   Исправление входа либо cancel/readback остаются поддерживаемыми путями.
3. Неизвестный persisted stage kind получает UNKNOWN_STEP_KIND с теми же
   правилами безопасного освобождения/блокировки.
4. Блокирующие операции не занимают event loop heartbeat. При отмене локальный
   producer учитывается до конца; продолжение отменённой coroutine не публикует
   результат, а запись из самой операции проходит исходный DB generation fence.
   Неиспользованный immutable объект может остаться без DB pointer.
5. Local OS read/upload ошибки становятся безопасными StorageError, в логе
   появилась error_category без текста исключения и сырых данных.
6. Модельный порт явно расширен describe/readiness/prepare. Пользовательский
   адаптер с одним generate должен реализовать эти операции; GenerateOnly теперь
   отвергается mypy. Старые DTO/import paths остаются совместимыми.

Нет изменений схемы/EAV/journal version/checkpoint format и нет Alembic migration.
Wire fixtures не перезаписывались. Byte-level identities остаются предметом
golden и restart tests, а не следствием самого переноса классов.

## Известные проблемы вне ТЗ

- Общий run timeout включает ожидание человека; timeout policy не исправлялась.
- Повторное восстановление source workspace имеет прежний функциональный дефект;
  извлечение SourceArtifacts и offload не объявляются его исправлением.
- Пересогласование отклонённого плана требует отдельной функциональной задачи.
- Нет live qualification облака/S3/сетевой изоляции в этой работе. HTTP transport,
  runner service и gateway тестируются с детерминированными заменами внешней
  инфраструктуры; это не production proof.
- Человеческие M10/T14 не выполнены. Полный technical profile также остаётся
  обязательным открытым gate.

Переход в READY/production, merge и deployment этой работой не выполнялись.
[Матрица требований](requirements.md) сохраняет BLOCKED там, где доказательства нет.

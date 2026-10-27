# Проверка рефакторинга для сопровождения человеком

Дата: 27.10.2026. Baseline: `bea8639471537fb9c181089cdb027d69e31a041b`.
Проверенная опубликованная реализация:
[`50dedd5f760c9b75c6d216a35c9e4a29414a9983`](https://github.com/rtalyutin/v2c/commit/50dedd5f760c9b75c6d216a35c9e4a29414a9983).
Ветка: `codex/human-maintainability`, [draft PR №1](https://github.com/rtalyutin/v2c/pull/1).
Объём: M01–M09 из [ТЗ](maintenance/specification.md);
для M10 подготовлен [протокол](maintenance/human-acceptance.md), человеческое
исполнение не проведено.

## Текущая проверка

Локальный финальный профиль:

```sh
V2C_BROWSER_EXECUTABLE=/absolute/path/to/chromium python scripts/check.py --web
python scripts/lock.py --check
python -m compileall -q src tests scripts deploy migrations
git diff --check
```

| Проверка | Фактический результат |
|---|---|
| Ruff lint и format | PASS, 99 Python-файлов |
| Strict mypy | PASS, 53 source files; в том числе реальные runner/provider/settings соответствуют портам |
| Imports, cycles, локальные ссылки, OpenAPI drift | PASS; 15 Markdown-документов |
| Python suite | **267 passed, 3 skipped, 1 warning**, 40,58 с |
| SQLite upgrade + Alembic schema comparison | PASS |
| Prettier и настоящий браузер | PASS; **8 scenarios**, 3,8 с |
| Dependency locks, compileall, diff check | PASS |
| Чистая установка, wheel, настоящий localhost Uvicorn | PASS; health/readiness/OpenAPI 200, auth 401/200, web 7/7, unknown resource 404 |

Локально отсутствуют PostgreSQL и Docker Compose: пропущены два PostgreSQL-теста
(конкуренция; fencing/reconciliation/rollback) и настоящий Compose env test.
Полный CI настроен с этими зависимостями и отклоняет любой skipped case.
**Полный CI PASS:** [run 40509217223](https://github.com/rtalyutin/v2c/actions/runs/40509217223),
[job 122206371486](https://github.com/rtalyutin/v2c/actions/runs/40509217223/job/122206371486).
Head — `50dedd5f760c9b75c6d216a35c9e4a29414a9983`; фактический PR checkout —
`3f28f83d4e0d29ef7b5c2e3ae1c85dcbaa56bf53`, merge с baseline `bea8639`.
Git tree обеих ревизий и локальной опубликованной версии совпал:
`1bccd382e23f82acbca7226dcb7be3fa5b0c533b`.

| Полный CI | Фактический результат из job log |
|---|---|
| Установка из lock и проверка свежести locks | PASS |
| Ruff / strict mypy / import boundaries / docs / OpenAPI | PASS: 99 Python-файлов, 53 typed source files, 15 Markdown-документов |
| Python suite, включая оба PostgreSQL-теста и настоящий Compose env test | **270 passed, 0 skipped, 1 warning**, 42,25 с |
| PostgreSQL upgrade и schema comparison, SQLite upgrade и comparison | PASS; обе проверки не обнаружили drift |
| Prettier / Chromium | PASS; **8 scenarios**, 4,8 с |
| Отдельный `docker compose config --quiet` | PASS |
| `docker build --tag v2c:ci .` | PASS |

Локальный идентификатор собранного CI-образа:
`sha256:0ee7c0f3af5ced7da8beb66c100b6c0ff5c3399e0ed235ff3358e8e31d08ef9d`.
Образ собран в runner Actions; публикация в registry и развёртывание не заявляются.
Результат подтверждён чтением шагов и полного журнала независимым проверяющим ИИ.
Последующие изменения этого отчёта и TASK_STATE не меняют проверенный код;
актуальные проверки последних документационных коммитов доступны в PR.

Среда: Linux x86_64, Python 3.12.14, закреплённые Python/Node dependencies.
Локальный Chromium 153.0.8010.0 указан через документированный override.
Стандартный download Playwright в этой среде получил повреждённый архив; первая
попытка общего прогона без override завершилась ошибкой запуска всех восьми
браузерных случаев, затем полный профиль с установленным браузером прошёл.
В CI используется штатный браузер закреплённого Playwright.

Чистая установка проверена в отдельной копии без исходного venv, cache, build,
данных или `.env`: фактический `python scripts/bootstrap_dev.py`, `pip check`,
сборка и установка wheel, Alembic и настоящий HTTP. Это отдельная копия на момент
проверки; окончательный suite выполнен на интегрированном исходнике.

Типы не объявляются полной типизацией всего legacy-кода: точные исключения
указаны в pyproject. Разнородный JSON декодеров/контекста модели сохранён в
узких сериализующих участках; claims, fences, limits, порты и конкретные формы
recipe/check/stage results имеют именованные типы без изменения wire-значений.

## Сохранение контрактов и независимые проверки

- Изолированный коммит форматирования имеет тот же AST для всех 33 затронутых
  Python-файлов; исходные тесты: 194 passed, 2 skipped.
- 29 реальных ответов operator API сравниваются с immutable fixtures исходной
  версии: HTTP status, JSON/null, фильтрация, заголовки, ZIP, repeated approval,
  cancel и human handoff. Эти данные не перегенерировались по новому коду.
- Отдельные fixtures сохраняют формы runner/provider DTO, канонический JSON,
  request hashes и различие отсутствующего поля с `null`.
- Независимый проверяющий ИИ получил настоящие SQLite-журналы запуском старого
  кода: новая версия сохранила UNKNOWN без повтора эффекта, pending delete и
  данные; более новая схема отклонена без перезаписи. Это дополнение к
  `test_journal_versions.py`, а не проверка живой VM.
- Отрицательные пробы проверяют отказ при ошибке типа, Ruff, нарушении imports,
  цикле, устаревшем OpenAPI и незапланированной колонке ORM. Ошибка исходного
  import-checker для package `__init__.py` найдена ревью и исправлена.
- Fault probes проверяют неизменность исходного исключения, состояний и числа
  вызовов; секреты из exception message не попадают в структурированный лог.

## Известные ограничения

M10 не выполнялся человеком. Улучшение времени onboarding или скорости доработки
ещё не измерено. Состояние задачи: **техническая реализация с отдельным,
неподтверждённым человеческим gate**.

Защита `main` не применена: при последнем чтении `protected=false`, rulesets
отсутствовали. Предложение обязательного PR и check `Maintainer checks` находится
в [операционной инструкции](maintenance/operations.md). CI YAML не является
свидетельством включённой защиты ветки. Слияния и развёртывания не выполнялись.

Настоящие Timeweb create/boot/delete, модельный провайдер, S3 и внешняя сетевая
изоляция в рамках рефакторинга не испытывались. Платные ресурсы не создавались.
Локальные порты/fixtures и PostgreSQL/Compose в CI не заменяют эти проверки.
Сохраняются ограничения внешнего сетевого профиля и модельного free-profile,
описанные в [предыдущем отчёте](verification-before-maintenance.md).

Есть прежнее предупреждение Starlette TestClient/httpx. Его устранение потребует
отдельного согласованного обновления тестового транспорта; оно не скрывается
фильтром warnings.

Независимое ревью не обнаружило оставшихся блокирующих дефектов в проверенном
локальном исходнике. 119 файлов кода/тестов/конфигурации/схем и их режимы не
изменились во время финального прогона. SHA-256 набора:
`f33616c04b5401c066c3afd5f1d2074d5ae4d5f4f88169b2f62609a611cd6436`.
Отдельная дифференциальная проверка 210 вариантов настроек против baseline
подтвердила совпадение результатов и ошибок. Это ревью ИИ, не человеческий M10.

Исторические результаты предыдущей версии сохранены в
[verification-before-maintenance.md](verification-before-maintenance.md).

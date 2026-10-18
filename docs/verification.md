# Проверка MVP

Дата: 18.10.2026. Объём: расширение baseline `62af78c` — Timeweb gateway, runner template, mTLS/SNI и бесплатный модельный профиль. Это проверка инструмента, а не приёмка клиентского проекта.

## Наблюдаемые результаты

- Локальный прогон перед первой публикацией: независимый `.venv/bin/python -m pytest -q -rs`, **194 passed, 2 skipped, 1 warning in 17.37s**. Исторический отпечаток 54 runtime/test/config/deploy/примеров/README/Timeweb-doc файлов совпал до и после того прогона: `3109e4f44ab45b7edb9825aeda4c3b3be42ea7aab90cf79b10b113ec5e4b4b8d`. Изменяемый отчёт `docs/verification.md` в отпечаток не входит; последующее исправление тестовой конфигурации меняет отпечаток.
- Пропуски: PostgreSQL concurrency без `V2C_TEST_POSTGRES_URL` и настоящий Compose env regression без Docker. CI настроен с PostgreSQL 16 и Docker; результат конкретного SHA следует читать в GitHub Actions.
- Результат CI опубликованной ревизии следует читать в GitHub Actions; локальные результаты приведены ниже. Зелёный CI baseline `62af78c` не является проверкой текущих изменений.
- Первая публикация `be9b455` прошла PostgreSQL migration, но [CI 39158216670](https://github.com/rtalyutin/v2c/actions/runs/39158216670) завершился **1 failed, 195 passed, 1 warning in 15.13s**, без пропусков: настоящий Compose-тест получил пустой обязательный `V2C_POSTGRES_PASSWORD` из временного `.env`. Шаги отдельной Compose-проверки и Docker build после pytest были пропущены. Узкое исправление заполняет в тестовом `.env` синтетические PostgreSQL password и API token; обязательные проверки продукта и настоящий вызов Compose сохранены. Результат CI исправленной ревизии проверяется отдельно.
- Выполнены настоящие локальные mTLS-handshake по IP с DNS SAN/SNI, включая отказ неверному имени. Подмена allocation/profile не даёт READY. Это локальный TLS-сервер, а не Timeweb VM.
- Выполнены офлайн генерация временных сертификатов и рендер cloud-init/systemd. Потерянный ответ create, чужое удаление, firewall readback и cleanup проверены управляемыми HTTP/VM-портами. Подтверждённые CREATE 401/403 освобождают слот без повторного CREATE; timeout/5xx/некорректный 201 сохраняют UNKNOWN и занятый слот. Provision/run deadline приводит к удалению именно записанной CREATING VM до безопасного ожидания.
- Три настоящих DTO pipeline прошли ограниченный Groq wire-профиль на маленьком исходнике через настоящий адаптер с тестовым HTTP-транспортом. Это проверяет сериализацию и границы, но не поведение провайдера.
- Root повторно выполнил `V2C_MODEL_PRESET=groq-free-gpt-oss-120b python -m v2c.model_probe`: exit 2, `NEEDS_KEY`, `network_request_performed=false`, `provider_exchange_verified=false`. Живого подключения модели нет: `GROQ_API_KEY` отсутствует.
- Python compileall и git diff check пройдены. Baseline ранее прошёл временную SQLite/Alembic миграцию и настоящий Uvicorn HTTP smoke; они не подменяют проверку новых облачных компонентов.

Предупреждение — deprecation Starlette TestClient/httpx. Визуальный браузерный проход панели не выполнен.

## Границы фактической готовности

Реальные Timeweb create/boot/delete, Docker isolation, внешний firewall, S3 и генерация Groq не испытывались. Cloud/API-ключи и доступ к VM не предоставлены; платные ресурсы не создавались.

Внешний профиль Timeweb — default DROP с двумя явными reserved TCP/9 egress-исключениями. Это не буквальный нулевой egress. Фактическая маршрутизация этих адресов и ответный трафик после lockdown требуют live network gate; локальные тесты не означают cloud security PASS. Guest firewall/unreachable routes не заменяют внешнюю границу.

Groq preset предназначен для smoke и малых изменений: 6000 байт полного запроса, 1024 токена ответа. Free eligibility и тариф конкретного аккаунта не выводятся из названия пресета. Код не меняет тариф и не переключает провайдера; 429 переводит запуск в ожидание ввода вместо расходования всех попыток. Успех большого проекта не заявляется.

LeraBack и второй незнакомый проект, независимая ИИ-доработка, приёмка разработчиком и стоимость настоящего запуска остаются этапами PoC. Зелёные тесты инструмента не дают клиентскому проекту READY.

Настройки: [timeweb.md](timeweb.md), протокол: [runner-protocol.md](runner-protocol.md), базовое развёртывание: [deployment.md](deployment.md).

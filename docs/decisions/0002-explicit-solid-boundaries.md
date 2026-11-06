# ADR 0002: явные порты, admission и владельцы атомарных операций

Дата: 2026-11-06. Статус: реализовано в локальной SOLID-ветке; полная техническая
и независимая человеческая приёмка ещё BLOCKED.

## Причина

Прежний ModelPort обещал generate, но потребитель дополнительно требовал profile,
request_body и is_test. Pipeline смешивал сборку, стадии и attempt lifecycle;
Engine применял несколько независимых сценариев. Эти границы мешали замене
адаптера и локальному изменению поведения.

## Решение

- Канонические DTO и полные structural Protocol размещаются в contracts.
  Проверочные импорты реальных реализаций находятся в scripts/check_conformance.
- ModelPort явно задаёт describe/readiness/prepare/generate. Adapter владеет
  resolver и wire serialization; VM lifecycle ничего не знает о модели.
- Composition root явно регистрирует StageSpec. StageExecutor получает admission
  и handler из неизменяемого registry; StageHandlers описывает действия этапов.
  Package сохраняет прежнюю политику без модели, остальные шесть — с моделью.
- RunStore, LeaseStore и PlanStore получают одну ControlDatabase. Вызов write-порта
  представляет целую атомарную операцию, не несколько самостоятельно commit-ящих
  сервисов. Evidence/READY остаются у прежнего EvidenceStore.
- Consumer-owned порты ограничивают зависимости. Общие настройки разбираются в
  composition root, упаковка получает конкретные значения.
- Blocking work выполняется через run_blocking; отмена учитывает thread до
  завершения. DB fence запрещает публикацию старой generation. UNKNOWN внешнего
  эффекта сохраняет slot и необходимость reconciliation.

## Цена

Больше именованных границ и явных методов у адаптера модели. Внешнему адаптеру,
реализовавшему только generate, требуется дополнение контракта. Старые import
paths и constructors сохраняются фасадами; их удаление — отдельное изменение.

Не вводятся plugin scanner, DI framework, distributed transaction, параллельные
runs, новая schema или новый cloud provider. Ограничение размера файла не является
критерием. Нейтральные библиотеки схем допустимы; конкретные HTTP/ORM imports
запрещены в прикладных сервисах даже под TYPE_CHECKING.

## Проверка и ограничения

[Матрица](../solid/requirements.md), [контракты](../solid/ports.md),
[отчёт](../solid/verification.md). Автотесты с doubles не квалифицируют реальный
cloud/S3. PostgreSQL/Docker/browser и T14 остаются обязательными отдельными gates.

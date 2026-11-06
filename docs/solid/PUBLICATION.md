# Публикация SOLID-ветки

2026-11-06: владелец явно разрешил отправить изменения в main.
Ветка опубликована: `codex/solid-contracts-20261105`.
[PR #2](https://github.com/rtalyutin/v2c/pull/2) содержит актуальные SHA, состояние
CI, evidence полного технического профиля и результат слияния.

Локальные отчёты здесь фиксируют checkpoint **до публикации**. BLOCKED для
PostgreSQL/Docker/browser относится к окружению автора; результат последующего
GitHub CI фиксируется в описании PR на конкретном SHA. T14/M10 остаются
NOT EXECUTED даже после зелёного CI и разрешённого merge. Deployment не выполняется.

## Соответствие Git-объектов

Shell Git не имел write credentials. Содержание перенесено GitHub Git Data API
с сохранением отдельных commits. На каждом шаге опубликованный tree SHA точно
совпал с локальным. Родители, время и автор публикации дали новые commit SHA.

| Checkpoint | Локальный commit | Опубликованный commit |
|---|---|---|
| Финальный код и тесты | be5af35ce8915fb7164cd6efa55617ee26acebc8 | 1c6bd0709eefaab008d5ec62821a90de8e356480 |
| Документация и local evidence | 5134fe1ea9ca6535ead18a1f88230b49ffa25043 | 623abec85a6d76f40aa71646bcb2547a06f1521e |

Этот последующий commit уточняет только документацию публикации. Runtime,
тесты, зависимости, wire fixtures и миграции не меняются.

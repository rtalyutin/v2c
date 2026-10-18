# v2c на Timeweb Cloud

В репозитории реализован `v2c.timeweb_gateway`: он переводит протокол
создания/чтения/удаления runner VM в Timeweb Cloud API. Операторская VM
пользователя размещает управление; клиентские проекты исполняются на новых
одноразовых VM. Сам запуск gateway не создаёт VM. Конкретный аккаунт,
сервер, токен, сеть, bucket и модель этим документом не считаются настроенными.

## Что подготовить

| Объект | Точные данные |
| --- | --- |
| Управляющая VM | Ubuntu/Docker/Compose ≥2.24, доступ администратора, реальный публичный исходящий unicast IPv4 управления с маской `/32` |
| Timeweb API | JWT с правами создания/чтения VM и автоматического удаления; для удаления без подтверждения нужен `is_able_to_delete=true` |
| Runner VM | Проверенный `preset_id`, UUID базового `image_id`, при необходимости ID публичного SSH-ключа; Python 3.12+ и работающий cloud-init в образе |
| Runtime | Собранный под входной проект Docker-образ по digest; для тестовой БД — отдельный закреплённый PostgreSQL-образ и его UID/GID |
| S3 | Приватный bucket, его endpoint, access/secret keys, регион; поддержка условной записи и checksum проверяется отдельно |
| Модель | URL OpenAI-совместимого API, имя модели, ключ; фактическая совместимость проверяется одним синтетическим запросом |

Стартовая конфигурация для небольшого PoC с одним активным запуском:

| VM | ОС / архитектура | CPU / RAM / диск | Сеть |
| --- | --- | --- | --- |
| Управление: API, worker, PostgreSQL, gateway | Ubuntu 24.04 LTS, x86_64 | 4 vCPU / 8 GiB / 50 GiB SSD | Статический публичный IPv4; панель 8080 и gateway 9443 остаются на loopback, доступ к панели через SSH-туннель |
| Одноразовый runner | Ubuntu 24.04 LTS с cloud-init, x86_64 | 2 vCPU / 4 GiB / 30 GiB SSD | Публичный IPv4; TCP 8443 только от публичного `/32` управления; без общей приватной сети |

Это предложение стартовых ресурсов, а не измеренные минимумы. GPU не нужен:
модель вызывается по API. Runner создаётся отдельно для исполнения/свежей
проверки и удаляется; контейнерные лимиты задаются независимо от тарифа VM.
Для крупных зависимостей, образов и БД профиль нужно пересмотреть до запуска.

Значения `0`/`REPLACE_...` в `deploy/timeweb/*.example.json` — намеренные
заглушки, а не существующие ресурсы. IDs ОС/тарифов/образов из чужих примеров
не переносить в рабочую конфигурацию. Чтение каталогов доступно через
`GET /api/v1/presets/servers`, `/os/servers`, `/images`; базовый адрес API —
`https://api.timeweb.cloud`. `image_id` — UUID, `preset_id` — число.

Не создавать образ runner из управляющей VM: её диск может содержать
Timeweb, S3 и модельные ключи. Нужен отдельный проверенный Ubuntu 24.04+
cloud-init образ. Можно использовать импортированный чистый образ или
отдельно подготовленный образ runner. Работа cloud-init в выбранном образе
проверяется при реальном первом запуске; генератор не подтверждает boot.

## Приватные файлы и TLS

Ключ Timeweb хранится только в `gateway.json`, который монтируется только
в gateway. Не класть его в общий `.env`: этот файл читают API и worker.
Поле `ownership_secret` и журнал gateway нужно сохранять при перезапусках:
они связывают запрошенные allocation_id с принадлежащими шлюзу VM.

Офлайн PKI для ограниченного PoC можно создать локально, без облачных
запросов:

```sh
python deploy/timeweb/make_pki.py --output /etc/v2c-operator/pki --days 7
```

Каталог должен отсутствовать до запуска; родитель должен быть доступен
оператору. Генератор создаёт файлы 0600 в каталоге 0700. `ca.key` остаётся
вне контейнеров и VM. Для дальнейшей эксплуатации использовать принятый
у оператора процесс выпуска и ротации сертификатов.

| Получатель | Файлы |
| --- | --- |
| API/worker, каталог `control-client` | `ca.crt`, `client.crt`, `client.key` |
| Gateway, каталог `gateway` | `ca.crt`, `client.crt`, `client.key`, `gateway.crt`, `gateway.key` |
| Runner через приватный cloud-init | `runner.crt`, `runner.key`, только публичный `ca.crt` для проверки клиентов |

У runner сертификат с DNS SAN `runner.v2c.internal`; у gateway — другой
leaf-сертификат с SAN `timeweb-gateway.v2c.internal` и `timeweb-gateway`.
Контроль соединяется с фактическим публичным IP runner, передавая фиксированное
имя через SNI; CA и hostname продолжают проверяться. Покупать домен или
настраивать wildcard DNS для этого не требуется.

## Шаблон одноразовой VM

1. Скопировать `runner-settings.example.json` и `runner-build.example.json`
   в приватный каталог оператора вне репозитория. В settings задать
   случайный runner bearer token и digest образа. Для PostgreSQL дополнить
   `postgres_image`, `postgres_data_bytes`, `postgres_ready_seconds`,
   `postgres_uid`, `postgres_gid`; в базовом примере БД выключена.
2. В build указать HTTPS URL архива **этой же проверенной ревизии v2c**,
   независимо полученный SHA256 архива, пути к settings и TLS-файлам,
   публичный исходящий IPv4 управления с маской `/32`. Более широкие сети,
   private/reserved/multicast адреса этот MVP отклоняет. Путь с названием ветки `main`
   не фиксирует исходный код; использовать commit или immutable artifact.
3. Отрендерить закрытый шаблон:

```sh
python deploy/timeweb/render_runner.py \
  --config /etc/v2c-operator/runner-build.json \
  --output /etc/v2c-operator/gateway/runner-cloud-init.yaml
```

Рядом появится `runner-cloud-init.profile.json`. Значение
`runner_profile_sha256` скопировать в `expected_runner_profile_sha256`
конфигурации gateway. Хеш вычисляется самим `RunnerSettings`, а не вводится
вместо проверяемых настроек. Шаблон содержит literal-маркер
`__V2C_ALLOCATION_ID__`, который gateway заменяет на allocation_id при
создании. Созданный cloud-init содержит bearer token и приватный runner key;
он должен оставаться закрытым и не попадать в Git, общие логи или поставку
клиенту. Не передавать в него Timeweb/S3/модельные ключи, CA private key
или client private key.

При загрузке VM cloud-init устанавливает Docker/Python, сверяет SHA256
исходников до распаковки/установки, ставит закреплённые зависимости,
загружает runtime-образы по digest и создаёт отдельную ограниченную ext4
файловую систему workspace. Затем включается host firewall и systemd runner
с обязательным mTLS. Дальнейшее исполнение не скачивает образы: используется
`--pull=never`. Это подготовленный сценарий; реальная загрузка VM пока
требует проверки на выбранном образе и тарифе.

Для диска учитываются **workspace filesystem + PostgreSQL tmpfs**.
Например, 512 MiB workspace и 256 MiB БД требуют `V2C_RUNNER_DISK_MB=768`
или больше. CPU/RAM/PID лимит исполнения делится между приложением и БД.
Сам тариф VM также должен вмещать ОС, Docker и bootstrap; параметры
контейнера не являются размером всей VM.

## Внешний Firewall

Этот профиль использует публичный IPv4 runner и mTLS. Не подключать runner
в общую приватную сеть управления: **Timeweb Firewall не фильтрует приватные
сети**. В текущем gateway `network` в шаблоне сервера запрещён.

Оператор создаёт отдельную группу с default policy **DROP**. Если в направлении
нет правил, Timeweb не фильтрует это направление, поэтому профиль содержит
ровно три правила:

| Направление | Протокол/порт | CIDR | Назначение |
| --- | --- | --- | --- |
| ingress | tcp / 8443 | Фактический публичный CIDR управления | mTLS-запросы к runner |
| egress | tcp / 9 | `192.0.2.1/32` | Непустой список IPv4 egress |
| egress | tcp6 / 9 | `2001:db8::1/128` | Непустой список IPv6 egress |

Оба egress адреса — специальные документационные адреса IANA. Это
**default-deny с двумя явными исключениями**, а не буквальный deny-all.
Их фактическая достижимость/маршрутизация внутри Timeweb этим выпуском не
проверена. Bootstrap дополнительно устанавливает для них локальные
unreachable-маршруты. Guest firewall — дополнительная защита, не замена
внешнему ограничению.

Получить точный JSON из того же алгоритма, что проверяет gateway:

```sh
umask 077
python -m v2c.timeweb_gateway --print-firewall-rules \
  --control-cidr ВАШ_ПУБЛИЧНЫЙ_IP/32 --runner-port 8443 \
  > /etc/v2c-operator/gateway/firewall-rules.json
```

Эта команда не обращается к облаку. Нативные запросы настройки группы:

```text
POST /api/v1/firewall/groups?policy=DROP
  {"name":"v2c-runner-lockdown","description":"Operator-reviewed v2c profile"}
POST /api/v1/firewall/groups/{group_id}/rules
  <каждый из трёх объектов firewall-rules.json>
```

В ответе создания группы — `group.id`; его записать в gateway.json.
Создание группы/правил — внешнее изменение и здесь автоматически не
выполняется. `policy` передаётся query-параметром, не полем JSON.
Для IPv6 используется `tcp6` согласно официальному Timeweb CLI; некоторые
генерированные SDK имеют более узкое устаревшее перечисление протоколов.

Gateway сначала ждёт authenticated runner health с правильным allocation_id
и profile hash. После bootstrap он прикрепляет эту группу к новой VM через
`POST /api/v1/firewall/groups/{group_id}/resources/{server_id}?resource_type=server`,
читает привязанные группы и точные правила обратно, затем повторяет mTLS
health. Непроверенная привязка, лишняя группа, отличающиеся правила или
неработающий после lockdown ответ оставляют VM вне READY. Успешный API
ответ привязки сам по себе не доказывает прохождение реального трафика.

## Конфигурация gateway и управления

В приватный `/etc/v2c-operator/gateway` поместить:

```text
gateway.json
server-template.json
runner-cloud-init.yaml
firewall-rules.json
ca.crt
client.crt
client.key
gateway.crt
gateway.key
```

Скопировать `gateway.example.json` и `server-template.example.json`, заполнить
реальные IDs/ключи/хеш. Используемые внутри gateway пути начинаются с
`/run/v2c-timeweb/`, так как это путь bind mount в контейнере.
`server-template.json` задаёт только `preset_id`, `image_id` и опциональные
`ssh_keys_ids`, `project_id`, `availability_zone`. `name`, `comment`,
`cloud_init` и параметры подключения gateway создаёт сам. Не помещать
секреты в пример из репозитория.

Должны совпасть:

| Значение gateway.json | Значение управления/runner |
| --- | --- |
| `bearer_secret` | `V2C_VM_PROVIDER_TOKEN` в control `.env` |
| `runner_bearer_secret` | `V2C_RUNNER_TOKEN` и `runner-settings.json.bearer_secret` |
| `runner_server_name` | `V2C_RUNNER_SERVER_NAME` и runner certificate SAN |
| `control_cidr` | Публичный исходящий адрес VM управления и firewall-rules.json |
| `expected_runner_profile_sha256` | Хеш из результата render_runner.py |

Приватный gateway-каталог монтируется только в gateway. Контейнер работает
как UID/GID 10001: закрытый каталог должен быть доступен этому владельцу,
файлы — 0600. Аналогично подготовить отдельный `control-client` каталог
для CA/client certificate/key и указать его как `V2C_CERTS_DIR`.

В `.env` заполнить обычные настройки управления, S3 и ключи модели.
В `.env.timeweb` скопировать `.env.timeweb.example` с путями и URL:

```dotenv
V2C_TIMEWEB_CONFIG_DIR=/etc/v2c-operator/gateway
V2C_VM_PROVIDER_URL=https://timeweb-gateway:9443
V2C_RUNNER_SERVER_NAME=runner.v2c.internal
```

Timeweb API token в этих общих env-файлах отсутствует. Запуск Compose:

```sh
docker compose --env-file .env --env-file .env.timeweb \
  --profile timeweb-gateway config --quiet
docker compose --env-file .env --env-file .env.timeweb \
  --profile timeweb-gateway up --build -d
docker compose --env-file .env --env-file .env.timeweb \
  --profile timeweb-gateway logs --tail=100 migrate api worker timeweb-gateway
```

Запуск сервиса не проверяет доступность облачного аккаунта и не создаёт VM.
При `/v1/vms` gateway сохраняет intent до одного POST создания. При
неизвестном исходе он ищет принадлежащую ему VM по HMAC-маркеру и не
повторяет создание из-за отсутствия результата списка. Одновременно
допускается одна активная/неразрешённая VM. Удаление подтверждается чтением
состояния; неизвестный исход не освобождает слот и не считается удалением.

## S3 и модель

В Timeweb реквизиты bucket взять из его настроек. Официальный пример для
одного из кластеров: endpoint `https://s3.twcstorage.ru`, region `ru-1`;
реальный endpoint/регион выбранного bucket проверяется в панели.
В control `.env` задать `V2C_STORAGE_BACKEND=s3`, `V2C_S3_ENDPOINT_URL`,
`V2C_S3_BUCKET`, `V2C_S3_PREFIX`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, при необходимости `AWS_DEFAULT_REGION`.
Дать отдельному ключу доступ к нужному prefix, включая Put/Get/Head.
Настройки видимости bucket должны оставаться приватными.

Документация Timeweb подтверждает работу boto3, но не доказывает
совместимость используемых нами `IfNoneMatch`/`ChecksumSHA256`. Перед
первым проектом отдельно проверить на выделенном тестовом ключе запись,
чтение, head, отказ перезаписать существующий объект и SHA256 readback.
Отсутствие этих возможностей — блокировка S3-поставки, а не разрешение
тихо ослабить неизменяемость артефактов.

В `.env.timeweb.example` выбран профиль `groq-free-gpt-oss-120b`.
Для него добавить `GROQ_API_KEY` только в control `.env`: endpoint, модель
и имя переменной ключа задаёт профиль. В `.env.timeweb` ключ не дублировать:
пустое значение из второго env-файла перекроет заполненное значение первого.
Сам профиль не подтверждает бесплатный тариф аккаунта; его нужно проверить
у провайдера. Автоматического перехода на платную модель нет.

Другой провайдер подключается после удаления `V2C_MODEL_PRESET`: тогда
задать `V2C_MODEL_BASE_URL`, `V2C_MODEL`, `V2C_MODEL_API_KEY_ENV`
и соответствующую переменную ключа. Смешивание preset с ручными полями
блокируется как конфликт конфигурации.

После решения оператора сделать один запрос совместимости можно выполнить:

```sh
docker compose --env-file .env --env-file .env.timeweb \
  --profile timeweb-gateway exec worker python -m v2c.model_probe
```

Это реальный запрос к выбранному провайдеру и может тарифицироваться.
Успех проверяет небольшой text/JSON Schema обмен, а не качество
рефакторинга проекта. Текущий wire profile использует Chat Completions,
`max_tokens` и строгий `response_format=json_schema`; другие контракты
моделей подключаются новым адаптером/профилем.

Холодный запуск VM имеет отдельный deadline: профиль Timeweb задаёт
`V2C_PROVISION_TIMEOUT_SECONDS=1800` и интервал проверок
`V2C_PROVISION_POLL_SECONDS=5`. Это время bootstrap/lockdown, а не таймаут
команды проекта, который остаётся 120 секунд. Provision также ограничен
общим `V2C_RUN_TIMEOUT_SECONDS=3600`: каждый этап не получает отдельный
часовой бюджет. Фактическое завершение проекта за этот срок не обещается.
Превышение срока не доказывает отсутствие созданной VM: нужны
reconciliation и подтверждённая очистка.

## Перед реальным PoC

Локальные и CI проверки не создают Timeweb VM. Нужен отдельный разрешённый
сквозной прогон: свежая VM → bootstrap → две health-проверки вокруг
lockdown → команда → checkpoint/восстановление тестовой БД → подтверждённое
удаление. До него нельзя объявлять boot, cloud firewall, S3 или внешний
модельный обмен проверенными. Пять попыток на шаг не задают денежный лимит.

Первичные источники, проверенные при подготовке:

- [Timeweb API](https://timeweb.cloud/tools/api-docs).
- [Официальная OpenAPI, закреплённая ревизия SDK-go](https://github.com/timeweb-cloud/sdk-go/blob/49ea2e2e42d9729bb940c6461df502a51129bca2/api/openapi.yaml).
- [Официальный CLI: протоколы и policy](https://github.com/timeweb-cloud/twc/blob/e4aacf3ed081630ebfa288779a707b91c4a954f8/twc/api/types.py).
- [Timeweb Firewall: направления и приватные сети](https://timeweb.cloud/docs/firewall/upravlenie-fajrvolom).
- [Timeweb cloud-init](https://timeweb.cloud/docs/cloud-servers/manage-servers/cloud-init).
- [Timeweb образы](https://timeweb.cloud/docs/cloud-servers/manage-servers/server-images).
- [Timeweb boto3/S3](https://timeweb.cloud/docs/s3-storage/sdk/python).
- [IANA IPv4 special-purpose registry](https://www.iana.org/assignments/iana-ipv4-special-registry/iana-ipv4-special-registry.xhtml).
- [IANA IPv6 special-purpose registry](https://www.iana.org/assignments/iana-ipv6-special-registry/iana-ipv6-special-registry.xhtml).

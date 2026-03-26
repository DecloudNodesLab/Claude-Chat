# Claude Workspace

Self-hosted веб-интерфейс для работы с Anthropic Claude API в Docker-контейнере.

## Описание

Claude Workspace — это минималистичный, полностью рабочий веб-интерфейс для Claude, который запускается в одном Docker-контейнере без дополнительных привилегий. Интерфейс предоставляет чат с Claude, загрузку файлов и встроенный терминал в браузере.

## Возможности

- **Чат с Claude** — полноценный диалог с историей, несколькими беседами
- **Инструменты Claude** — Claude может читать/писать файлы, просматривать директории и выполнять команды в `/workspace`
- **Загрузка файлов** — загрузка файлов с компьютера напрямую в `/workspace`
- **Встроенный терминал** — полнофункциональный PTY-терминал в браузере (xterm.js)
- **Общий shell-контекст** — команды Claude видны в терминале пользователя
- **Двуязычный интерфейс** — русский и английский языки
- **Basic Auth** — защита доступа логином и паролем
- **Одиночный контейнер** — не требует Docker Compose, не требует привилегий

## Архитектура

```
Браузер
  │
  ├── HTTP  ──► FastAPI (app/main.py)
  │               ├── Basic Auth (app/auth.py)
  │               ├── Jinja2 шаблон (templates/index.html)
  │               ├── Чат → Claude API (app/chat.py)
  │               │   └── Tools (app/tools.py)
  │               ├── Загрузка файлов → /workspace
  │               └── История → JSON файлы в /data/chats/
  │
  └── WS  ────► PTY Shell (app/shell.py)
                  └── /bin/bash в /workspace
```

## Структура каталогов

```
claude-workspace/
├── Dockerfile
├── .dockerignore
├── docker-compose.yml
├── entrypoint.sh
├── requirements.txt
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI приложение, маршруты
│   ├── auth.py        # Basic Auth
│   ├── chat.py        # Интеграция с Claude API
│   ├── shell.py       # PTY + WebSocket терминал
│   ├── tools.py       # Инструменты для Claude
│   ├── storage.py     # Хранение чатов в JSON
│   └── i18n.py        # Локализация (ru/en)
└── templates/
    └── index.html     # Единственная HTML-страница
```

**Директории контейнера:**

| Путь | Назначение |
|------|-----------|
| `/workspace` | Рабочая директория: файлы пользователя, скрипты, данные проекта |
| `/data` | Служебные данные приложения: история чатов (`/data/chats/*.json`) |

## Локальный запуск без Docker

Требования: Python 3.10+, bash

```bash
# Клонировать / распаковать проект
cd claude-workspace

# Создать виртуальное окружение
python -m venv venv
source venv/bin/activate

# Установить зависимости
pip install -r requirements.txt

# Создать рабочие директории
mkdir -p /workspace /data/chats
# или задать через переменные:
export WORKSPACE_DIR=./workspace
export DATA_DIR=./data
mkdir -p ./workspace ./data/chats

# Задать переменные окружения
export ANTHROPIC_API_KEY=sk-ant-...
export CLAUDE_MODEL=claude-opus-4-5
export BASIC_AUTH_USERNAME=admin
export BASIC_AUTH_PASSWORD=secret

# Запустить
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Открыть в браузере: http://localhost:8000

## Сборка Docker image

```bash
cd claude-workspace

docker build -t claude-workspace:latest .
```

## Запуск контейнера

### Минимальный запуск:

```bash
docker run -d \
  --name claude-workspace \
  -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e BASIC_AUTH_USERNAME=admin \
  -e BASIC_AUTH_PASSWORD=secret123 \
  claude-workspace:latest
```

### С постоянным хранением данных:

```bash
docker run -d \
  --name claude-workspace \
  -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e CLAUDE_MODEL=claude-opus-4-5 \
  -e BASIC_AUTH_USERNAME=admin \
  -e BASIC_AUTH_PASSWORD=secret123 \
  -e DEFAULT_LOCALE=ru \
  -v ./my-workspace:/workspace \
  -v ./my-data:/data \
  claude-workspace:latest
```

### Через Docker Compose:

```bash
# Создать .env файл
cat > .env << EOF
ANTHROPIC_API_KEY=sk-ant-...
BASIC_AUTH_USERNAME=admin
BASIC_AUTH_PASSWORD=secret123
DEFAULT_LOCALE=ru
EOF

docker compose up -d
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `ANTHROPIC_API_KEY` | *(обязательная)* | API ключ Anthropic |
| `CLAUDE_MODEL` | `claude-opus-4-5` | Модель Claude |
| `BASIC_AUTH_USERNAME` | `admin` | Логин для входа |
| `BASIC_AUTH_PASSWORD` | `changeme` | Пароль для входа |
| `APP_HOST` | `0.0.0.0` | Адрес сервера |
| `APP_PORT` | `8000` | Порт сервера |
| `DEFAULT_LOCALE` | `en` | Язык по умолчанию (`en` или `ru`) |
| `WORKSPACE_DIR` | `/workspace` | Рабочая директория |
| `DATA_DIR` | `/data` | Директория данных приложения |

## Как открыть интерфейс

После запуска откройте в браузере: **http://localhost:8000**

Браузер запросит логин и пароль (HTTP Basic Auth). Введите значения `BASIC_AUTH_USERNAME` и `BASIC_AUTH_PASSWORD`.

## Работа с терминалом

Терминал внизу страницы — полноценный PTY bash-терминал в браузере:

- Рабочая директория по умолчанию: `/workspace`
- Поддерживает цвета, Tab-дополнение, историю команд (`↑`/`↓`)
- Размер адаптируется автоматически и при перетаскивании разделителя
- При обрыве соединения переподключается автоматически

## Общий shell-контекст Claude и пользователя

Когда Claude использует инструмент `run_command`:

1. Команда инжектируется в PTY-терминал пользователя — видна в браузере
2. Одновременно выполняется в subprocess для надёжного захвата вывода
3. Результат (stdout/stderr/код возврата) возвращается Claude
4. В терминале виден комментарий `# Claude: <команда>` перед выполнением

Таким образом пользователь видит всё, что делает Claude.

## Ограничения MVP

- Нет выбора модели в UI (только через `CLAUDE_MODEL`)
- Нет потокового вывода ответов Claude (request/response)
- Нет управления файлами через UI (только через shell и Claude)
- Нет авторегистрации и управления пользователями
- Один пользователь (single-user deployment)
- История shell не сохраняется между перезапусками контейнера
- Без TLS/HTTPS (используйте reverse proxy в продакшене)

## Рекомендации по безопасности

1. **Обязательно смените пароль** — не используйте `changeme` в продакшене
2. **Используйте HTTPS** — поставьте nginx/caddy как reverse proxy с TLS
3. **Ограничьте доступ** — не открывайте порт 8000 напрямую в интернет
4. **Пример nginx:**
   ```nginx
   server {
     listen 443 ssl;
     server_name your-domain.com;
     location / {
       proxy_pass http://localhost:8000;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_set_header Host $host;
     }
   }
   ```
5. **API ключ** никогда не попадает во frontend — только на backend
6. **Изоляция** — контейнер не требует и не использует привилегированный режим

## Troubleshooting

**Терминал не подключается:**
- Проверьте логи: `docker logs claude-workspace`
- Убедитесь, что WebSocket-соединение не блокируется прокси
- Для nginx добавьте заголовки `Upgrade` и `Connection`

**Claude не отвечает:**
- Проверьте `ANTHROPIC_API_KEY`
- Проверьте, что модель `CLAUDE_MODEL` существует и доступна
- Откройте `/health` — должен вернуть `{"status":"ok"}`

**Ошибка загрузки файла:**
- Проверьте права доступа к `/workspace` внутри контейнера
- Убедитесь, что volume примонтирован с правами на запись

**Проблемы с PTY на некоторых системах:**
- Убедитесь, что контейнер запущен без `--read-only`
- Проверьте, что `/dev/ptmx` доступен (стандартно для Docker)

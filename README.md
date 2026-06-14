# Codex-Antigravity Bridge

Этот bridge позволяет Codex делегировать задачи в Google Antigravity CLI и получать ответ обратно.

На Windows `agy --print` сейчас может успешно завершаться, но возвращать пустой stdout при запуске из non-interactive subprocess. `agy_pty.py` обходит это через ConPTY (`pywinpty`): запускает `agy` как будто в настоящем терминале, читает терминальный вывод и отдает очищенный текст вызывающей стороне.

Итоговая схема:

```text
Codex -> MCP server -> agy_pty.py -> ConPTY -> agy.exe -> Antigravity
```

Bridge определяет возможности установленного `agy` через capability probe. На Windows по умолчанию используется PTY backend, потому что прямой capture stdout для `agy -p` может быть пустым даже при успешном ответе модели.

## Установка

```powershell
python -m pip install --user -r requirements.txt
```

Также нужен установленный Antigravity CLI:

```powershell
where.exe agy
agy --version
agy update
```

## Быстрая Проверка

Проверка низкоуровневого wrapper:

```powershell
python agy_pty.py "What is 2+2? Answer with one digit only." --timeout=60s --json
```

Ожидаемый `output`:

```json
"4"
```

Проверка MCP server через stdio:

```powershell
python mcp_server.py
```

В обычной консоли эта команда будет ждать MCP-клиента. Для реальной проверки запускай ее через Codex MCP config или тестовый MCP client.

## Выбор Модели

Antigravity CLI 1.0.5+ поддерживает официальный флаг:

```powershell
agy --model "Gemini 3.5 Flash (High)" -p "Reply with exactly: OK"
```

Bridge использует этот флаг автоматически, если capability probe видит `--model`. Это основной путь после обновления до `agy 1.0.6`.

Для старых версий CLI bridge сохраняет fallback через временное изменение:

```text
%USERPROFILE%\.gemini\antigravity-cli\settings.json
```

Порядок такой:

1. Bridge берет lock-файл рядом с `settings.json`.
2. Запоминает исходные настройки.
3. Временно выставляет `"model": "<label>"`.
4. Запускает `agy`.
5. Возвращает только исходное поле `model`, не перезаписывая весь `settings.json`.

Если процесс был жестко убит во время fallback model override, следующий запуск bridge увидит crash marker рядом с `settings.json` и восстановит старое значение `model`, если текущая модель все еще совпадает с оставленным override.

На `agy 1.0.6` `settings.json` для выбора модели не меняется.

Диагностический пример:

```powershell
python agy_pty.py "Reply with exactly: OK" --model "Gemini 3.5 Flash (High)"
```

Список моделей bridge берет из `agy models` через PTY. Если CLI недоступен, используется fallback:

```text
models.json
```

## MCP Server

Codex config:

```toml
[mcp_servers.antigravity]
command = "python"
args = ["C:\\path\\to\\codex-antigravity-bridge\\mcp_server.py"]
enabled = true
required = false
startup_timeout_sec = 60
tool_timeout_sec = 600
```

Tools:

- `antigravity_delegate`: передать prompt в Antigravity и вернуть ответ.
- `antigravity_delegate_async`: запустить делегацию в фоне и вернуть `job_id`.
- `antigravity_run_status`: проверить статус async job.
- `antigravity_get_summary`: получить summary async job.
- `antigravity_get_result_path`: получить путь к полному результату async job.
- `antigravity_smoke_test`: проверить полный путь, ожидает `MCP_OK`.
- `antigravity_capabilities`: показать версию `agy`, flags/subcommands и recommended backend.
- `antigravity_current_settings`: показать non-secret настройки bridge и trusted roots.
- `antigravity_list_models`: показать model labels из `agy models`, с fallback на `models.json`.

`antigravity_delegate` поддерживает `return_mode`:

| Значение | Что возвращается в Codex |
|---|---|
| `full` | Полный ответ Antigravity inline + файл `result.md` |
| `file_summary` | Только короткий summary, метаданные и путь к `result.md`; полный ответ остается в файле |

Для экономии токенов Codex используй `file_summary`:

```json
{
  "return_mode": "file_summary",
  "summary_max_bullets": 5
}
```

Артефакты запусков пишутся сюда:

```text
<cwd>/.agentboard/antigravity/runs/
```

Эту папку не нужно коммитить. Bridge автоматически хранит только последние 50 запусков в каждом workspace.

Capability probe:

```json
{
  "include_live_probe": false
}
```

`include_live_probe: false` не тратит LLM-квоту: bridge читает только `agy --version` и `agy --help`. `include_live_probe: true` отправляет маленький smoke prompt и проверяет direct stdout против PTY backend.

Prompt guardrails:

- жесткий лимит: 12000 символов после добавления `mode: "advise"` wrapper-текста;
- предупреждение: после 8000 символов;
- при превышении лимита запрос не отправляется в Antigravity, а в run directory пишется только truncated preview.

## Перенос В Другой Проект

Есть два режима переноса: project-local и global.

### Project-Local

В этом режиме bridge лежит внутри каждого проекта.

1. Скопировать файлы репозитория в папку вашего проекта, например:

```text
tools/antigravity-bridge/
```

2. В новом проекте установить зависимости:

```powershell
python -m pip install --user -r tools\antigravity-bridge\requirements.txt
```

3. Проверить Antigravity CLI:

```powershell
where.exe agy
agy --version
```

4. Добавить локальные папки в `.gitignore` нового проекта:

```gitignore
.antigravitycli/
.agentboard/
__pycache__/
```

5. Один раз запустить smoke-test из корня нового проекта:

```powershell
python tools\antigravity-bridge\agy_pty.py "Reply with exactly: OK" --add-dir "<ABSOLUTE_PROJECT_PATH>" --timeout=60s
```

6. Добавить MCP server в `%USERPROFILE%\.codex\config.toml`, заменив путь проекта:

```toml
[mcp_servers.antigravity]
command = "python"
args = ["<ABSOLUTE_PROJECT_PATH>\\codex-antigravity-bridge\\mcp_server.py"]
enabled = true
required = false
startup_timeout_sec = 60
tool_timeout_sec = 600
```

7. Перезапустить Codex, чтобы он перечитал MCP config.

8. В новом проекте вызвать `antigravity_smoke_test`.

### Global MCP

В этом режиме bridge лежит в одном месте, например:

```text
C:\Users\username\.codex\tools\codex-antigravity-bridge\
```

А рабочий проект передается через `cwd`/`add_dirs` в каждом MCP-вызове. Чтобы bridge знал, какие workspace разрешены, добавь env-переменные в MCP config:

```toml
[mcp_servers.antigravity]
command = "python"
args = ["C:\\Users\\username\\.codex\\tools\\codex-antigravity-bridge\\mcp_server.py"]
enabled = true
required = false
startup_timeout_sec = 60
tool_timeout_sec = 600

[mcp_servers.antigravity.env]
ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT = "C:\\path\\to\\your\\project"
ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS = "C:\\path\\to\\your\\project"
```

`ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT` задает default `cwd`, если tool-вызов не передал `cwd`. `ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS` задает дополнительные разрешенные корни; несколько путей разделяются стандартным разделителем PATH для ОС (`;` на Windows).

Если env-переменные не заданы, fallback остается project-local:

```text
BRIDGE_DIR.parent.parent
```

Run artifacts всегда пишутся в workspace, то есть в `<resolved_cwd>/.agentboard/`, а не рядом с глобальным bridge.

## Что Проверить При Переносе

Если `agy` пишет ошибку про symlink:

```text
A required privilege is not held by the client
```

включи Windows Developer Mode и перезапусти терминал/Codex. Antigravity использует `.antigravitycli/` как локальную project-ссылку на свои настройки в `%USERPROFILE%\.gemini\config\projects`.

Если `antigravity_delegate` возвращает пустой ответ:

- не вызывай `agy --print` напрямую;
- проверь `agy_pty.py` smoke-test;
- проверь, что установлен `pywinpty`;
- проверь, что Antigravity CLI авторизован.
- если `pywinpty` сломан или отсутствует, bridge вернет diagnostic warning и попробует subprocess fallback; на Windows такой fallback может не вернуть stdout.

Если запрос отклонен по размеру:

- не вставляй файлы целиком;
- передай пути;
- попроси Antigravity читать только конкретные файлы/секции.

Если нужна другая модель:

- вызови `antigravity_list_models`;
- передавай точный label в `model`;
- если `agy models` не работает, обнови fallback `models.json`.

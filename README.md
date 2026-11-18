# AI-Codex-Tools

Локальные инструменты для OpenAI Codex на Windows. Работают сами по себе; парный репозиторий —
[AI-Claude-Tools](https://github.com/Valerii-Hrymalskyi/AI-Claude-Tools): его MCP-сервер
`task-delegator` поручает задачи Codex и берёт отсюда скрипт лимитов.

| Что | Что делает |
|---|---|
| [`Projects/GetUsage`](Projects/GetUsage/README.md) | `codex_usage.py` — лимиты Codex (окна 5h и 7d) аккаунта, в который вошёл локальный Codex; квоту не тратит. Отчёт в консоль, JSON с контрактом `schema: 1`, Stop-хук для Codex Desktop и Claude Code |

## Что нужно

- **Windows 10/11.**
- **Codex** — Codex Desktop или Codex CLI с выполненным входом (`codex.exe` скрипт находит сам).
- **Доступ к `api.nuget.org`** при первой установке: оттуда скачивается портативный Python 3.15.2
  (SHA-256 закреплён). Системный Python не нужен. Node.js нужен лишь для старого моста
  `codex-usage-mcp.js`.

## Начало работы

Ниже `<clone>` — папка клона, `<python>` — `<clone>\Utils\Bin\Windows\Python3\python.exe`.

1. **Python.** Двойной клик по `python_setup.bat` (или `python_setup.ps1` из PowerShell).
   Скачивает портативный Python закреплённой версии с NuGet, проверяет SHA-256 и ставит его в
   `Utils\Bin\Windows\Python3` вместе с пакетами из `python-requirements.txt` (сейчас их нет:
   скриптам хватает стандартной библиотеки). В git он не хранится. Повторный запуск безопасен;
   `-Force` — переустановить. Тот же скрипт стоит в AI-Claude-Tools.
2. **Проверка.** `& "<python>" "<clone>\Projects\GetUsage\codex_usage.py"` должен напечатать
   строку `Codex limits: 5h: …`.
3. **По желанию — Stop-хук**, чтобы видеть лимиты после каждого ответа Codex или Claude:
   [GetUsage/README.md](Projects/GetUsage/README.md#stop-hooks).
4. **Вместе с AI-Claude-Tools** — клонировать оба репозитория в одну родительскую папку:
   TaskDelegator ищет скрипт лимитов в соседней папке `AI-Codex-Tools` (или `Codex`). В другом
   месте — задайте серверу `TASK_DELEGATOR_CODEX_USAGE_SCRIPT`.

## Структура

Та же раскладка, что в AI-Claude-Tools:

```
python_setup.bat            запуск установки Python двойным кликом
python_setup.ps1            установка Python: версия и SHA-256 закреплены в начале файла
python-requirements.txt     точный набор пакетов (pip freeze; сейчас пусто)
Projects/
  GetUsage/                 скрипт лимитов Codex, его тесты и README; старый мост codex-usage-mcp.js
Utils/Bin/Windows/Python3/  портативный Python (ставится скриптом, в .gitignore)
.github/workflows/tests.yml CI: офлайн-тесты на windows-latest
```

## Проверка

```powershell
& "<python>" "<clone>\Projects\GetUsage\test_codex_usage.py" -v
```

Тесты офлайновые: настоящий Codex не нужен, квота не тратится. Их же гоняет CI
(`.github/workflows/tests.yml`).

## Обновить Python

Поменять `$Version` и `$Sha256` в `python_setup.ps1` (хэш сверить с SHA-512 из каталога NuGet) и
запустить `python_setup.bat -Force`. Python, которым пользуется работающий процесс (например,
Stop-хук Codex в момент запуска), заменить нельзя: скрипт откажется и покажет, кто его держит.
Скрипт одинаков в AI-Codex-Tools и AI-Claude-Tools — меняйте обе копии вместе.

---

## In English

Local tools for OpenAI Codex on Windows. They work on their own; the companion repository is
[AI-Claude-Tools](https://github.com/Valerii-Hrymalskyi/AI-Claude-Tools).

- **`Projects/GetUsage/codex_usage.py`** reads the 5-hour and weekly rate-limit windows of the
  account signed into the local Codex, without spending quota: a console report, schema-1 JSON
  (`--json`), and a Stop hook for Codex Desktop and Claude Code. The `task-delegator` MCP server of
  AI-Claude-Tools runs it before every Codex job.
- **Requirements:** Windows 10/11, a signed-in Codex Desktop or CLI, access to api.nuget.org on first
  setup.
- **Setup:** `python_setup.bat` installs a pinned portable Python 3.15.2 (SHA-256 checked) into
  `Utils\Bin\Windows\Python3`; the scripts need only its standard library. To use it with
  AI-Claude-Tools, clone both into one parent folder (TaskDelegator looks for a sibling
  `AI-Codex-Tools` or `Codex`), or point `TASK_DELEGATOR_CODEX_USAGE_SCRIPT` at the script. Details:
  [GetUsage/README.md](Projects/GetUsage/README.md).

License: MIT.

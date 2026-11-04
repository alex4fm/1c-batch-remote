# Ручной запуск Python-исполнителя

1. Установите 64-разрядный Python и добавьте его в PATH. Проверьте настройки в `settings.json`.
2. Откройте командную строку Windows (`cmd`) в папке `python-executor`.
3. Выполните команды по очереди. Для установки компонентов нужен доступ в интернет. Если команда завершилась ошибкой, устраните её перед следующей командой.

```cmd
python -m venv venv
venv\Scripts\python.exe -m pip install -r server\requirements.txt
venv\Scripts\python.exe -m pip check
venv\Scripts\python.exe -u server\server.py
```

При последующих запусках достаточно последней команды. Оставьте окно открытым; для остановки сервера нажмите **Ctrl+C**.

Также можно запустить `start-python_executor.cmd`: он выполняет подготовку окружения и запуск автоматически

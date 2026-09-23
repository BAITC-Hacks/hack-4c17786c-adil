"""Start the local Campaign Studio interface: python app.py --open."""
import argparse
import json
import threading
from urllib.request import urlopen
import webbrowser

from setup_case import restore
from webapp.server import Manager, make_server


def main():
    try:
        import numpy  # noqa: F401 - fail early with an actionable setup message
        import pandas  # noqa: F401
    except ImportError:
        raise SystemExit("Установите зависимости: python -m pip install -r requirements.txt")
    parser = argparse.ArgumentParser(description="Локальный интерфейс агентской системы")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Открыть интерфейс в браузере")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Порт должен быть от 1024 до 65535")
    address = f"http://127.0.0.1:{args.port}"
    # A second double-click should open the already running local app.
    try:
        with urlopen(address + "/api/health", timeout=0.5) as response:
            running = json.load(response)
        if isinstance(running, dict) and running.get("app") == "Campaign Studio":
            print("Приложение уже запущено: " + address)
            if args.open:
                webbrowser.open(address)
            return
    except (OSError, ValueError):
        pass
    try:
        created, _ = restore()
        print(f"Данные готовы. Восстановлено CSV: {len(created)}")
    except (OSError, ValueError) as exc:
        print(f"Данные требуют проверки: {exc}")
    try:
        manager = Manager()
    except OSError as exc:
        raise SystemExit("Не удалось открыть каталог истории .local/runs. Проверьте права записи и свободное место: " + str(exc)) from exc
    for warning in manager.history_warnings:
        print(warning)
    try:
        server = make_server(manager, port=args.port)
    except OSError as exc:
        manager.close()
        raise SystemExit(f"Не удалось открыть локальный порт {args.port}: {exc}. Попробуйте другой порт: python app.py --port 8766 --open") from exc
    print("Campaign Studio: " + address)
    print("Оставьте это окно открытым. Для остановки нажмите Ctrl+C.")
    if args.open:
        threading.Timer(0.3, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nЗавершение работы...")
    finally:
        server.server_close()
        manager.close()


if __name__ == "__main__":
    main()

"""Local HTTP API, persistent job history and bounded background execution."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"queued", "running"}
STATUSES = ACTIVE | {"completed", "failed", "cancelled", "interrupted"}
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ARTIFACTS = {
    "campaign_plan.csv": "План эксперимента · CSV",
    "submission.csv": "Файл для сдачи · seed 42",
    "report.html": "Отчёт · HTML",
    "report.json": "Отчёт агента · JSON",
    "result.json": "Полные результаты · JSON",
    "comparison.csv": "Сравнение прогонов · CSV",
    "tests.txt": "Результаты тестов · TXT",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json_object(path):
    """Do not let malformed saved state break every history/API request."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Ожидался JSON-объект.")
    # json.loads accepts NaN/Infinity (and an overflowing 1e999) by default.
    json.dumps(value, allow_nan=False)
    return value


def validate_config(payload):
    if not isinstance(payload, dict):
        raise ValueError("Настройки запуска должны быть JSON-объектом.")
    allowed = {"mode", "seed", "max_pilots", "time_limit_seconds", "runs"}
    if set(payload) - allowed:
        raise ValueError("Неизвестные настройки: " + ", ".join(sorted(set(payload) - allowed)))
    mode = payload.get("mode", "single")
    if not isinstance(mode, str) or mode not in {"single", "batch", "tests"}:
        raise ValueError("Режим должен быть single, batch или tests.")
    config = {"mode": mode}
    fields = {"seed": (42, 0, 2147483627), "max_pilots": (20, 1, 20),
              "time_limit_seconds": (240, 10, 240), "runs": (10 if mode == "batch" else 1, 2 if mode == "batch" else 1, 20 if mode == "batch" else 1)}
    for name, (default, low, high) in fields.items():
        value = payload.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name}: укажите целое число от {low} до {high}.")
        config[name] = value
    return config


class Manager:
    def __init__(self, root=ROOT, data_dir=None, worker_command=None, maxqueue=10):
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else self.root / ".local" / "runs"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.worker_command = list(worker_command) if worker_command is not None else [sys.executable, "-m", "webapp.worker"]
        self.maxqueue = maxqueue
        self.lock = threading.RLock()
        self.dataset_lock = threading.Lock()
        self.jobs = {}
        self.processes = {}
        self.pending = queue.Queue()
        self.closed = threading.Event()
        self.dataset_cache = None
        self.dataset_signature = None
        self.history_warnings = []
        for folder in self.data_dir.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not ID_PATTERN.fullmatch(folder.name):
                continue
            try:
                job = read_json_object(folder / "metadata.json")
                if (job.get("id") != folder.name or not isinstance(job.get("status"), str)
                        or job["status"] not in STATUSES or not isinstance(job.get("created_at"), str)
                        or not job["created_at"]):
                    raise ValueError("Некорректные идентификатор, статус или дата запуска.")
                job["config"] = validate_config(job.get("config"))
                job["mode"] = job["config"]["mode"]
                for key, default in (("progress", {}), ("summary", {}), ("events", [])):
                    job.setdefault(key, default)
                    if not isinstance(job[key], type(default)):
                        raise ValueError(f"Некорректное поле истории: {key}.")
                if not all(isinstance(event, dict) for event in job["events"]):
                    raise ValueError("Некорректный журнал событий.")
                if job.get("status") in ACTIVE:
                    job.update(status="interrupted", finished_at=now(), error="Приложение завершилось до окончания запуска. Запустите эксперимент повторно.")
                    self._save_terminal(job)
                self.jobs[folder.name] = job
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                self.history_warnings.append(f"История {folder.name} не прочитана: {exc}. Файлы сохранены без изменений.")
                continue
        self.thread = threading.Thread(target=self._work, name="campaign-job-queue", daemon=True)
        self.thread.start()

    def _save(self, job):
        write_json(self.data_dir / job["id"] / "metadata.json", job)

    def _save_terminal(self, job):
        """A full/read-only disk must not prevent cancellation or kill the queue."""
        try:
            self._save(job)
        except OSError as exc:
            note = f"Не удалось сохранить состояние на диск: {exc}"
            job["error"] = f"{job['error']} {note}" if job.get("error") else note

    def _public(self, job, detail=False):
        result = copy.deepcopy(job)
        result["artifacts"] = [{"name": name, "label": label, "url": f"/api/runs/{job['id']}/files/{name}"}
                               for name, label in ARTIFACTS.items() if (self.data_dir / job["id"] / name).is_file()]
        result["result"] = None
        if detail:
            result_path = self.data_dir / job["id"] / "result.json"
            if result_path.exists():
                try:
                    result["result"] = read_json_object(result_path)
                except (OSError, ValueError, RecursionError) as exc:
                    result["result_error"] = f"Не удалось прочитать сохранённый результат: {exc}"
        else:
            result.pop("events", None)
        return result

    def list_runs(self):
        with self.lock:
            return [self._public(job) for job in sorted(self.jobs.values(), key=lambda row: row["created_at"], reverse=True)]

    def get_run(self, run_id):
        with self.lock:
            if not ID_PATTERN.fullmatch(run_id) or run_id not in self.jobs:
                raise KeyError("Запуск не найден.")
            return self._public(self.jobs[run_id], detail=True)

    def create(self, payload):
        config = validate_config(payload)
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("Приложение завершает работу.")
            if sum(job["status"] in ACTIVE for job in self.jobs.values()) >= self.maxqueue:
                raise RuntimeError("Очередь заполнена. Дождитесь завершения текущих запусков.")
            run_id = uuid.uuid4().hex
            job = {"id": run_id, "mode": config["mode"], "status": "queued", "config": config,
                   "created_at": now(), "started_at": None, "finished_at": None, "error": None,
                   "progress": {"trial_index": 0, "trial_count": config["runs"], "pilot_index": 0,
                                "max_pilots": config["max_pilots"], "message": "Ожидание свободного исполнителя"},
                   "events": [], "summary": {}}
            (self.data_dir / run_id).mkdir()
            self._save(job)
            self.jobs[run_id] = job
            self.pending.put(run_id)
            return self._public(job, detail=True)

    def cancel(self, run_id):
        process = None
        with self.lock:
            if run_id not in self.jobs:
                raise KeyError("Запуск не найден.")
            job = self.jobs[run_id]
            if job["status"] in ACTIVE:
                job.update(status="cancelled", finished_at=now(), error=None)
                job["progress"]["message"] = "Остановлено пользователем"
                self._save_terminal(job)
                process = self.processes.get(run_id)
            response = self._public(job, detail=True)
        if process is not None and process.poll() is None:
            process.terminate()
        return response

    def artifact(self, run_id, filename):
        if not ID_PATTERN.fullmatch(run_id) or run_id not in self.jobs or filename not in ARTIFACTS:
            raise KeyError("Файл не найден.")
        path = self.data_dir / run_id / filename
        if not path.is_file() or path.is_symlink():
            raise KeyError("Файл не найден.")
        return path

    def _event(self, run_id, event):
        with self.lock:
            job = self.jobs[run_id]
            if job["status"] != "running":
                return
            event = {**event, "at": now()}
            job["events"] = (job["events"] + [event])[-250:]
            progress = job["progress"]
            for key in ("trial_index", "trial_count", "pilot_index", "max_pilots"):
                if key in event:
                    progress[key] = event[key]
            if event.get("type") == "pilot":
                progress["message"] = f"Пилот {progress['pilot_index']} из {progress['max_pilots']}: {event.get('target_tariff', '')} / {event.get('channel', '')}"
            elif event.get("message"):
                progress["message"] = str(event["message"])
            self._save(job)

    def _work(self):
        while not self.closed.is_set():
            try:
                run_id = self.pending.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    if self.jobs[run_id]["status"] != "queued":
                        continue
                    job = self.jobs[run_id]
                    job.update(status="running", started_at=now())
                    job["progress"]["message"] = "Подготовка данных"
                    self._save(job)
                self._execute(run_id)
            except Exception as exc:
                with self.lock:
                    job = self.jobs[run_id]
                    if job["status"] != "cancelled":
                        job.update(status="failed", finished_at=now(), error=f"{type(exc).__name__}: {exc}")
                        self._save_terminal(job)
            finally:
                self.pending.task_done()

    def _execute(self, run_id):
        config = self.jobs[run_id]["config"]
        directory = self.data_dir / run_id
        command = [*self.worker_command, "--output-dir", str(directory), "--config", json.dumps(config)]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with self.lock:
            self.processes[run_id] = process
            already_cancelled = self.jobs[run_id]["status"] == "cancelled"
        if already_cancelled:
            process.terminate()
        lines = queue.Queue()
        def read_output():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + (180 if config["mode"] == "tests" else config["runs"] * (config["time_limit_seconds"] + 30) + 30)
        error = None
        tail = []
        try:
            ended = False
            while not ended:
                if self.closed.is_set() or time.monotonic() >= deadline:
                    error = "Превышено время выполнения." if not self.closed.is_set() else "Приложение остановлено."
                    process.terminate()
                    break
                with self.lock:
                    if self.jobs[run_id]["status"] == "cancelled":
                        process.terminate()
                        break
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    ended = True
                    continue
                tail = (tail + [line.strip()])[-8:]
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("type"):
                        self._event(run_id, event)
                        if event["type"] == "error":
                            error = str(event.get("message", "Ошибка исполнителя."))
                except ValueError:
                    pass
            try:
                returncode = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=3)
            reader.join(timeout=1)
            result = None
            if (directory / "result.json").exists():
                result = read_json_object(directory / "result.json")
            with self.lock:
                job = self.jobs[run_id]
                if job["status"] != "cancelled":
                    success = returncode == 0 and isinstance(result, dict) and not error
                    if result and result.get("summary", {}).get("tests_passed") is False:
                        success = False
                        error = "Проверки завершились: есть упавшие тесты. Подробности в журнале."
                    job.update(status="completed" if success else "failed", finished_at=now(),
                               error=None if success else (error or "Исполнитель не сформировал результат. " + " ".join(tail)[-2000:]),
                               summary=result.get("summary", {}) if isinstance(result, dict) else {})
                    job["progress"]["message"] = "Готово" if success else "Запуск завершился с ошибкой"
                    self._save(job)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()
            with self.lock:
                self.processes.pop(run_id, None)

    def dataset(self):
        from setup_case import ARCHIVE_SHA256, DATA_FILES
        with self.dataset_lock:
            files, signature, errors = [], [], []
            archive = self.root / "vendor" / "beeline_case_participants.zip"
            for name in (*DATA_FILES, "vendor/beeline_case_participants.zip"):
                path = self.root / name
                try:
                    stat = path.stat() if path.is_file() else None
                except OSError as exc:
                    stat = None
                    errors.append(f"{name}: {exc}")
                signature.append((name, stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0))
                if name in DATA_FILES:
                    files.append({"name": name, "exists": stat is not None, "size": stat.st_size if stat else 0,
                                  "valid": False, "error": "Не проверен: нужен полный набор CSV и оригинальный архив."
                                  if stat else "CSV отсутствует или недоступен для чтения."})
            signature = tuple(signature)
            if signature == self.dataset_signature:
                return copy.deepcopy(self.dataset_cache)
            result = {"ready": all(row["exists"] for row in files), "files": files, "customers": 0,
                      "tariff_count": 0, "baseline_arpu": 0.0, "segments": {"arpu": [], "data": [], "call": []}, "tariffs": []}
            missing = [row["name"] for row in files if not row["exists"]]
            if missing:
                result["error"] = "Не найдены CSV: " + ", ".join(missing) + ". Нажмите «Подготовить данные»."
            if errors:
                result.update(ready=False, error="Не удалось прочитать файлы: " + "; ".join(errors))
            if result["ready"]:
                try:
                    # The application prepares this exact participant bundle and
                    # refuses modified CSVs. Check it without mutating a GET.
                    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
                        raise ValueError("Контрольная сумма пакета участника не совпадает. Восстановите архив из репозитория.")
                    with zipfile.ZipFile(archive) as source:
                        for row in files:
                            row["valid"] = (self.root / row["name"]).read_bytes() == source.read(row["name"])
                            row["error"] = None if row["valid"] else "CSV изменён и отличается от оригинального пакета участника."
                    modified = [row["name"] for row in files if not row["valid"]]
                    if modified:
                        raise ValueError("Изменены CSV: " + ", ".join(modified) + ". Сохраните копии этих файлов и уберите их из каталога проекта, затем нажмите «Подготовить данные».")
                    import pandas as pd
                    profile = pd.read_csv(self.root / "customer_profile.csv")
                    tariffs = pd.read_csv(self.root / "data" / "dict_tariff.csv")
                    result.update(customers=len(profile), tariff_count=len(tariffs), baseline_arpu=float(profile["predicted_arpu"].sum()),
                                  tariffs=json.loads(tariffs.to_json(orient="records")))
                    for kind in ("arpu", "data", "call"):
                        result["segments"][kind] = [{"name": str(name), "count": int(count)} for name, count in profile[kind + "_segment"].value_counts().items()]
                except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
                    result.update(ready=False, error=f"Не удалось прочитать данные: {exc}")
                    for row in files:
                        if not row["valid"] and row["error"].startswith("Не проверен:"):
                            row["error"] = f"Не проверен: {exc}"
            self.dataset_cache, self.dataset_signature = result, signature
            return copy.deepcopy(result)

    def prepare_data(self):
        from setup_case import restore
        with self.lock:
            if any(job["status"] in ACTIVE for job in self.jobs.values()):
                raise RuntimeError("Дождитесь окончания запусков перед подготовкой данных.")
            try:
                restore(self.root, self.root / "vendor" / "beeline_case_participants.zip")
            except FileExistsError as exc:
                raise ValueError("CSV изменены и не были перезаписаны. Сохраните их копии и уберите изменённые файлы из каталога проекта перед восстановлением. " + str(exc)) from exc
            self.dataset_signature = None
        return self.dataset()

    def overview(self):
        runs = self.list_runs()
        return {"app": {"name": "Campaign Studio", "version": "1.0"}, "dataset": self.dataset(),
                "limits": {"budget": 100000, "contacts": 15000, "pilots": 20, "per_campaign": 5000, "max_campaigns": 10},
                "runs": {"total": len(runs), "completed": sum(run["status"] == "completed" for run in runs),
                         "active": sum(run["status"] in ACTIVE for run in runs), "failed": sum(run["status"] in {"failed", "interrupted"} for run in runs)},
                "latest_run": next((run for run in runs if run["status"] == "completed" and run["mode"] != "tests"), None),
                "recent_runs": runs[:5], "history_warnings": list(self.history_warnings)}

    def close(self):
        self.closed.set()
        for run in self.list_runs():
            if run["status"] in ACTIVE:
                self.cancel(run["id"])
        self.thread.join(timeout=5)


def make_server(manager, host="127.0.0.1", port=8765):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Эта версия предназначена для локального запуска на 127.0.0.1.")

    class Handler(BaseHTTPRequestHandler):
        server_version = "CampaignStudio/1.0"

        def log_message(self, format, *args):
            pass

        def _headers(self, status, content_type, size, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _trusted(self):
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "").lower() not in allowed:
                self._json(403, {"error": "Недопустимый адрес приложения."})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + item for item in allowed}:
                self._json(403, {"error": "Запрос разрешён только из интерфейса этого приложения."})
                return False
            return True

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._trusted():
                return
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            try:
                if path == "/api/health":
                    return self._json(200, {"app": "Campaign Studio", "status": "ok"})
                if path == "/api/overview":
                    return self._json(200, manager.overview())
                if path == "/api/runs":
                    return self._json(200, {"runs": manager.list_runs()})
                match = re.fullmatch(r"/api/runs/([0-9a-f]{32})", path)
                if match:
                    return self._json(200, {"run": manager.get_run(match[1])})
                match = re.fullmatch(r"/api/runs/([0-9a-f]{32})/files/([^/]+)", path)
                if match:
                    file = manager.artifact(match[1], match[2])
                    body = file.read_bytes()
                    content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    disposition = "attachment" if file.suffix != ".html" or "download" in parse_qs(parsed.query) else "inline"
                    self._headers(200, content_type + ("; charset=utf-8" if content_type.startswith("text/") or file.suffix == ".json" else ""), len(body),
                                  {"Content-Disposition": f'{disposition}; filename="{file.name}"'})
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                static = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
                if path in static:
                    file = manager.root / "web" / static[path]
                    if not file.is_file():
                        raise KeyError("Файл интерфейса не найден.")
                    body = file.read_bytes()
                    self._headers(200, (mimetypes.guess_type(file.name)[0] or "text/plain") + "; charset=utf-8", len(body))
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                raise KeyError("Страница не найдена.")
            except KeyError as exc:
                self._json(404, {"error": str(exc.args[0])})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._json(500, {"error": f"Не удалось выполнить запрос: {type(exc).__name__}: {exc}"})

        def do_POST(self):
            if not self._trusted():
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self._json(415, {"error": "Используйте application/json."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Некорректный размер JSON-запроса.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("NaN и Infinity не разрешены.")))
                if not isinstance(payload, dict):
                    raise ValueError("Тело запроса должно быть JSON-объектом.")
                path = unquote(urlsplit(self.path).path)
                if path == "/api/runs":
                    return self._json(202, {"run": manager.create(payload)})
                match = re.fullmatch(r"/api/runs/([0-9a-f]{32})/cancel", path)
                if match:
                    if payload:
                        raise ValueError("Остановка запуска принимает пустой JSON-объект {}.")
                    return self._json(200, {"run": manager.cancel(match[1])})
                if path == "/api/data/prepare":
                    if payload:
                        raise ValueError("Подготовка данных принимает пустой JSON-объект {}.")
                    return self._json(200, {"dataset": manager.prepare_data()})
                return self._json(404, {"error": "Действие не найдено."})
            except (ValueError, UnicodeError, RecursionError) as exc:
                self._json(400, {"error": str(exc)})
            except KeyError as exc:
                self._json(404, {"error": str(exc.args[0])})
            except RuntimeError as exc:
                self._json(409, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"Не удалось выполнить действие: {type(exc).__name__}: {exc}"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server

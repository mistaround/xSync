import argparse
import hmac
import json
import os
import posixpath
import re
import secrets
import tempfile
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paramiko

from .connections import Browser, endpoint_path, profile, scan_key, server_address
from .filesystem import WSL_PREFIX, is_wsl, operate, remote_endpoint, wsl_path
from .transfers import Job, MODES, TransferQueue, tools_for_job, verify_name


ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MAX_PROFILES = 100


def write_json(path, values):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(values, file, ensure_ascii=False, indent=2)
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


class Favorites:
    def __init__(self, directory):
        self.file = directory / "favorites.json"
        self.values = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}
        self.lock = threading.Lock()

    def list(self, endpoint):
        with self.lock:
            return list(self.values.get(endpoint, []))

    def change(self, endpoint, path, remove=False):
        with self.lock:
            paths = self.values.get(endpoint, [])
            if remove:
                paths = [item for item in paths if item != path]
            elif path not in paths:
                if len(paths) >= 64:
                    raise ValueError("每个连接最多保存 64 个收藏。")
                paths = [*paths, path]
            values = {**self.values, endpoint: paths}
            write_json(self.file, values)
            self.values = values

    def forget(self, endpoint):
        with self.lock:
            if endpoint in self.values:
                values = {key: paths for key, paths in self.values.items() if key != endpoint}
                write_json(self.file, values)
                self.values = values


class Profiles:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file = directory / "connections.json"
        self.lock = threading.Lock()
        self.hosts = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}

    def list(self):
        with self.lock:
            return list(self.hosts.values())

    def get(self, identifier):
        with self.lock:
            return self.hosts[identifier] if identifier else None

    def save(self, data):
        identifier = data.get("id") or uuid.uuid4().hex
        if not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier):
            raise ValueError("Invalid profile identifier.")
        record = profile(data, identifier)
        with self.lock:
            if identifier not in self.hosts and len(self.hosts) >= MAX_PROFILES:
                raise ValueError("The connection list is full.")
            self.write({**self.hosts, identifier: record})
        return record

    def remove(self, identifier):
        with self.lock:
            updated = self.hosts.copy()
            del updated[identifier]
            self.write(updated)

    def write(self, updated):
        write_json(self.file, updated)
        self.hosts = updated


class Application:
    def __init__(self, directory):
        self.profiles = Profiles(directory)
        self.favorites = Favorites(directory)
        self.browser = Browser()
        self.jobs = TransferQueue()
        self.network_slots = threading.BoundedSemaphore(8)

    def request(self, path, data):
        if path == "/api/config":
            return {"hosts": self.profiles.list(), "home": str(Path.home()),
                    "tools": {name: bool(tool) for name, tool in tools_for_job().items()}}
        if path in {"/api/list", "/api/scan"}:
            with self.network_slots:
                if path == "/api/scan":
                    return scan_key(*server_address(data["host"], data.get("port", 22)))
                return self.browse(data.get("host"), data.get("path"), data.get("force", True))
        if path == "/api/hosts/save":
            return self.profiles.save(data)
        if path == "/api/hosts/remove":
            self.remove_host(data["id"])
            return {}
        if path == "/api/jobs":
            return self.jobs.list()
        if path == "/api/jobs/cancel":
            self.jobs.cancel(data["id"])
            return {}
        if path == "/api/jobs/start":
            return self.start_job(data)
        raise KeyError("Unknown endpoint.")

    def start_job(self, data):
        return self.jobs.add(self.create_job(data))

    def browse(self, endpoint, path, force=False):
        if is_wsl(endpoint):
            resolved = wsl_path(endpoint, path or "")
            try:
                result = self.browser.list(None, resolved)
            except OSError as error:
                if getattr(error, "winerror", None) not in (53, 67) or not resolved.lower().startswith("\\\\wsl.localhost\\"):
                    raise
                result = self.browser.list(None, "\\\\wsl$\\" + resolved[len("\\\\wsl.localhost\\"):])
            result["parent"] = wsl_path(endpoint, result["parent"])
            return result
        return self.browser.list(self.profiles.get(endpoint), path, force)

    def file_operation(self, endpoint, operation, paths, name=""):
        if is_wsl(endpoint):
            paths = [wsl_path(endpoint, path) for path in paths]
        operate(self.profiles.get(remote_endpoint(endpoint)), operation, paths, name)

    def remove_host(self, endpoint):
        self.profiles.remove(endpoint)
        self.favorites.forget(endpoint)

    def close(self):
        self.jobs.close()
        self.browser.close()

    def create_job(self, data):
        protocol = data.get("protocol", "auto")
        if protocol not in MODES:
            raise ValueError("Invalid transfer protocol.")
        source_id, dest_id = data.get("source"), data.get("destination")
        source = self.profiles.get(remote_endpoint(source_id))
        dest = self.profiles.get(remote_endpoint(dest_id))
        src_path = wsl_path(source_id, data["path"]) if is_wsl(source_id) else endpoint_path(data["path"], bool(source))
        dest_dir = wsl_path(dest_id, data["directory"]) if is_wsl(dest_id) else endpoint_path(data["directory"], bool(dest))
        source_paths = posixpath if source else os.path
        dest_paths = posixpath if dest else os.path
        name = verify_name(source_paths.basename(src_path), windows=not dest and os.name == "nt")
        target = dest_paths.join(dest_dir, name)
        if not source and not dest:
            src = Path(src_path).resolve()
            dst = Path(target).resolve()
            if src == dst or src in dst.parents:
                raise ValueError("Choose a destination outside the source.")
        if source and dest and (source["host"], source["port"]) == (dest["host"], dest["port"]):
            if target == src_path or target.startswith(src_path + "/"):
                raise ValueError("Choose a destination outside the source.")
        workspace = data.get("workspace", "")
        if not isinstance(workspace, str) or len(workspace) > 64:
            raise ValueError("Invalid workspace.")
        direct = data.get("direct") is True and source and dest and protocol in {"auto", "rsync"}
        direct_key = endpoint_path(data["direct_key"], True) if direct and data.get("direct_key") else ""
        job = Job(source, src_path, dest, target, protocol, data.get("overwrite") is True,
                  workspace, direct, direct_key)
        if is_wsl(source_id):
            job.update(source="WSL · " + source_id.removeprefix(WSL_PREFIX))
        if is_wsl(dest_id):
            job.update(destination="WSL · " + dest_id.removeprefix(WSL_PREFIX))
        return job


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, *_args):
        pass

    def respond(self, payload, content_type, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, payload, status=HTTPStatus.OK):
        self.respond(json.dumps(payload).encode(), "application/json; charset=utf-8", status)

    def allowed_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.allowed_host():
            self.json({"error": "Invalid host."}, HTTPStatus.FORBIDDEN)
        elif self.path in ASSETS:
            file, content_type = ASSETS[self.path]
            self.respond(Path(__file__).with_name("web").joinpath(file).read_bytes(), content_type)
        else:
            self.json({"error": "Page not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        supplied = self.headers.get("Authorization", "").encode()
        if not self.allowed_host() or not hmac.compare_digest(supplied, self.server.authorization):
            self.json({"error": "Open xSync using the URL printed by the local app."}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self.json({"error": "Expected JSON."}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32_768:
                raise ValueError("Invalid request length.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            self.json(self.server.app.request(self.path, data))
        except KeyError as exc:
            self.json({"error": f"Unknown item or missing field: {exc}"}, HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, OSError, EOFError, RuntimeError, paramiko.SSHException) as exc:
            self.json({"error": str(exc) or type(exc).__name__}, HTTPStatus.BAD_REQUEST)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", port), Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def default_data_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "xSync"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "xsync"


def main():
    parser = argparse.ArgumentParser(description="xSync local file transfer workspace")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    with Server(args.port) as server:
        server.app = Application(args.data_dir)
        token = secrets.token_urlsafe(32)
        server.authorization = f"Bearer {token}".encode()
        url = f"http://127.0.0.1:{server.server_port}/#{token}"
        print(f"xSync is running at {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.app.close()


if __name__ == "__main__":
    main()

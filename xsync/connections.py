import base64
import errno
import hashlib
import ipaddress
import os
import posixpath
import re
import socket
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import paramiko
from paramiko.sftp import CMD_CLOSE, CMD_HANDLE, CMD_NAME, CMD_OPENDIR, CMD_READDIR


TIMEOUT = 12
LIST_LIMIT = 2000


def server_address(host, port):
    host = str(host).strip().strip("[]")
    if "%" in host:
        raise ValueError("IPv6 scope identifiers are not supported.")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        host = host.encode("idna").decode("ascii").lower()
        if len(host) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.rstrip(".").split(".")
        ):
            raise ValueError("Enter a valid hostname or IP address.") from None
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return host, port


def fingerprint(key):
    encoded = base64.b64encode(hashlib.sha256(key.asbytes()).digest())
    return "SHA256:" + encoded.decode().rstrip("=")


def scan_key(host, port):
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        with paramiko.Transport(sock) as transport:
            transport.start_client(timeout=TIMEOUT)
            key = transport.get_remote_server_key()
            return {"type": key.get_name(), "key": key.get_base64(), "fingerprint": fingerprint(key)}


def profile(data, profile_id):
    host, port = server_address(data["host"], data.get("port", 22))
    name = str(data["name"]).strip()
    user = str(data["user"]).strip()
    if not name or len(name) > 80 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", user):
        raise ValueError("Enter a connection name and a valid SSH username.")
    identity = str(data.get("identity", "")).strip()
    if identity:
        identity = str(Path(identity).expanduser().resolve())
        if not Path(identity).is_file():
            raise ValueError("The private key file does not exist on this computer.")
    host_key = data["hostKey"]
    key = paramiko.PKey.from_type_string(host_key["type"], base64.b64decode(host_key["key"], validate=True))
    return {
        "id": profile_id, "name": name, "host": host, "port": port, "user": user,
        "identity": identity,
        "hostKey": {"type": key.get_name(), "key": key.get_base64(), "fingerprint": fingerprint(key)},
    }


def known_name(host):
    return host["host"] if host["port"] == 22 else f'[{host["host"]}]:{host["port"]}'


@contextmanager
def ssh_client(host):
    pinned = host["hostKey"]
    key = paramiko.PKey.from_type_string(pinned["type"], base64.b64decode(pinned["key"]))
    with paramiko.SSHClient() as client:
        client.get_host_keys().add(known_name(host), key.get_name(), key)
        client.connect(
            host["host"], port=host["port"], username=host["user"],
            key_filename=host["identity"] or None, look_for_keys=not host["identity"],
            timeout=TIMEOUT, banner_timeout=TIMEOUT, auth_timeout=TIMEOUT, channel_timeout=TIMEOUT,
        )
        yield client


@contextmanager
def sftp_client(host, *, bulk=False):
    with ssh_client(host) as client:
        transport = client.get_transport()
        options = {"window_size": 8 * 1024 * 1024} if bulk else {}
        with paramiko.SFTPClient.from_transport(transport, **options) as sftp:
            sftp.get_channel().settimeout(TIMEOUT)
            yield sftp


def endpoint_path(value, remote):
    if not isinstance(value, str) or not value or len(value) > 8192 or "\x00" in value:
        raise ValueError("Enter a valid path.")
    if remote:
        if not value.startswith("/"):
            raise ValueError("Remote paths must start with /.")
        return "/" + posixpath.normpath(value).lstrip("/")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Enter an absolute local path.")
    return os.path.abspath(path)


def entry_kind(mode):
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def read_remote_directory(sftp, path, limit, check=None):
    # Paramiko's listdir_iter can leave request records and open directory handles.
    # Using its synchronous requests keeps both bounded when reusing a channel.
    kind, message = sftp._request(CMD_OPENDIR, path)
    if kind != CMD_HANDLE:
        raise paramiko.SFTPError("Expected a directory handle.")
    handle = message.get_binary()
    entries = []
    try:
        while True:
            if check:
                check()
            try:
                kind, message = sftp._request(CMD_READDIR, handle)
            except EOFError:
                return entries, False
            if kind != CMD_NAME:
                raise paramiko.SFTPError("Expected directory entries.")
            for _ in range(message.get_int()):
                name = message.get_text()
                longname = message.get_text()
                info = paramiko.SFTPAttributes._from_msg(message, name, longname)
                if name in ("", ".", "..") or "/" in name:
                    continue
                if len(entries) == limit:
                    return entries, True
                entries.append(info)
    finally:
        sftp._request(CMD_CLOSE, handle)


def list_files(host, value, sftp=None):
    if host and sftp is None:
        with sftp_client(host) as connection:
            return list_files(host, value, connection)
    entries = []
    truncated = False
    if sftp is not None:
        path = endpoint_path(value, True) if value else sftp.normalize(".")
        items, truncated = read_remote_directory(sftp, path, LIST_LIMIT)
        for item in items:
            entries.append({"name": item.filename, "path": posixpath.join(path, item.filename),
                            "kind": entry_kind(item.st_mode or 0), "size": item.st_size or 0})
        parent = posixpath.dirname(path.rstrip("/")) or "/"
    else:
        path = endpoint_path(value or str(Path.home()), False)
        with os.scandir(path) as listing:
            for item in listing:
                if len(entries) == LIST_LIMIT:
                    truncated = True
                    break
                try:
                    info = item.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                entries.append({"name": item.name, "path": item.path,
                                "kind": entry_kind(info.st_mode), "size": info.st_size})
        parent = str(Path(path).parent)
    entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold()))
    return {"path": path, "parent": parent, "entries": entries, "truncated": truncated}


class BrowseConnection:
    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.active = 0
        self.touched = time.monotonic()
        self.context = None
        self.sftp = None
        self.cache = OrderedDict()
        self.home = ""

    def close(self):
        context, self.context = self.context, None
        self.sftp = None
        if context:
            context.__exit__(None, None, None)

    def list(self, value, force):
        with self.lock:
            if force:
                self.cache.clear()
            path = endpoint_path(value, True) if value else self.home
            cached = self.cache.get(path)
            if cached and time.monotonic() - cached[0] < 8:
                self.cache.move_to_end(path)
                return cached[1]
            for attempt in range(2):
                try:
                    if self.sftp is None:
                        context = sftp_client(self.host)
                        self.sftp = context.__enter__()
                        self.context = context
                    result = list_files(self.host, path, self.sftp)
                    break
                except (EOFError, OSError, paramiko.SSHException) as exc:
                    if isinstance(exc, OSError) and exc.errno in (errno.ENOENT, errno.EACCES, errno.ENOTDIR):
                        raise
                    self.close()
                    if attempt or isinstance(exc, (paramiko.BadHostKeyException, paramiko.AuthenticationException)):
                        raise
            if not value:
                self.home = result["path"]
            self.cache[result["path"]] = (time.monotonic(), result)
            self.cache.move_to_end(result["path"])
            while len(self.cache) > 6:
                self.cache.popitem(last=False)
            return result


class Browser:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    def list(self, host, value, force=False):
        if not host:
            return list_files(None, value)
        key = (host["id"], host["host"], host["port"], host["user"], host["identity"], host["hostKey"]["key"])
        with self.lock:
            self.prune(time.monotonic())
            connection = self.entries.get(key)
            if connection is None and len(self.entries) >= 8:
                oldest = next((item for item in self.entries if not self.entries[item].active), None)
                if oldest is not None:
                    self.entries.pop(oldest).close()
            pooled = connection is not None or len(self.entries) < 8
            if connection is None:
                connection = BrowseConnection(host)
            if pooled:
                self.entries[key] = connection
                self.entries.move_to_end(key)
            connection.active += 1
        try:
            return connection.list(value, force)
        finally:
            with self.lock:
                connection.active -= 1
                connection.touched = time.monotonic()
                if not pooled:
                    connection.close()

    def prune(self, now):
        for key, item in list(self.entries.items()):
            if not item.active and now - item.touched > 90:
                item.close()
                del self.entries[key]

    def close(self):
        with self.lock:
            for entry in self.entries.values():
                entry.close()
            self.entries.clear()

import base64
import hashlib
import ipaddress
import os
import posixpath
import re
import socket
import stat
from contextlib import contextmanager
from pathlib import Path

import paramiko


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
def sftp_client(host):
    with ssh_client(host) as client:
        with client.open_sftp() as sftp:
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


def list_files(host, value):
    entries = []
    if host:
        with sftp_client(host) as sftp:
            path = sftp.normalize(".") if not value else endpoint_path(value, True)
            for i, item in enumerate(sftp.listdir_iter(path, read_aheads=4)):
                if i == LIST_LIMIT:
                    break
                if item.filename in (".", ".."):
                    continue
                entries.append({
                    "name": item.filename, "path": posixpath.join(path, item.filename),
                    "kind": entry_kind(item.st_mode or 0), "size": item.st_size or 0,
                })
            else:
                i = -1
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        truncated = i == LIST_LIMIT
    else:
        path = endpoint_path(value or str(Path.home()), False)
        truncated = False
        with os.scandir(path) as listing:
            for i, item in enumerate(listing):
                if i == LIST_LIMIT:
                    truncated = True
                    break
                try:
                    info = item.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                entries.append({
                    "name": item.name, "path": item.path,
                    "kind": entry_kind(info.st_mode), "size": info.st_size,
                })
        parent = str(Path(path).parent)
    entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold()))
    return {"path": path, "parent": parent, "entries": entries, "truncated": truncated}

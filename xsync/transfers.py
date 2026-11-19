import errno
import os
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath

import paramiko

from .connections import TIMEOUT, known_name, read_remote_directory, sftp_client


CHUNK = 256 * 1024
READ_REQUESTS = 128
RSYNC_PROGRESS = re.compile(rb"^\s*([\d,]+)\s+(\d+)%\s+([\d.]+)([kMGT]?)B/s\s+(\d+):(\d{2}):(\d{2})")
MAX_PENDING = 32
MAX_HISTORY = 100
MODES = {"auto", "rsync", "sftp", "scp"}
ACTIVE = {"queued", "running"}


class Cancelled(Exception):
    pass


def verify_name(name, *, windows=os.name == "nt"):
    if not name or name in (".", "..") or "/" in name or (windows and "\\" in name) or "\0" in name:
        raise ValueError("The transfer contains an unsupported file name.")
    if windows and (
        any(c in name for c in '<>:"|?*') or name.endswith((" ", ".")) or PureWindowsPath(name).is_reserved()
    ):
        raise ValueError(f"Windows cannot store the file {name!r}.")
    return name


class Filesystem:
    def __init__(self, sftp=None):
        self.sftp = sftp
        self.paths = posixpath if sftp else os.path

    def info(self, path):
        return self.sftp.lstat(path) if self.sftp else os.lstat(path)

    def exists(self, path):
        return self.info_or_none(path) is not None

    def info_or_none(self, path):
        try:
            return self.info(path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise

    def names(self, path, check):
        return (name for name, _ in self.entries(path, check))

    def entries(self, path, check):
        if self.sftp:
            entries, truncated = read_remote_directory(self.sftp, path, 20_000, check)
            if truncated:
                raise ValueError("SFTP directory exceeds 20,000 entries. Split the transfer or use rsync.")
            yield from ((item.filename, item if item.st_mode is not None else None) for item in entries)
        else:
            with os.scandir(path) as listing:
                for item in listing:
                    yield item.name, item.stat(follow_symlinks=False)

    def open(self, path, mode):
        if self.sftp:
            return self.sftp.open(path, "wxb" if mode == "xb" else mode)
        return open(path, mode)

    def mkdir(self, path):
        return self.sftp.mkdir(path) if self.sftp else os.mkdir(path)

    def remove(self, path):
        return self.sftp.remove(path) if self.sftp else os.unlink(path)

    def publish(self, temp, target, overwrite):
        if self.sftp:
            if overwrite and self.exists(target):
                self.sftp.posix_rename(temp, target)
            else:
                self.sftp.rename(temp, target)
        elif overwrite:
            os.replace(temp, target)
        elif os.name == "nt":
            os.rename(temp, target)
        else:
            os.link(temp, target)
            os.unlink(temp)


def stream_read(reader, remote, size, check):
    if not remote or size is None:
        while True:
            check()
            block = reader.read(CHUNK)
            if not block:
                return
            yield block
    else:
        packet_size = min(reader.MAX_REQUEST_SIZE, 32 * 1024)
        window = packet_size * READ_REQUESTS
        for start in range(0, size, window):
            check()
            end = min(size, start + window)
            requests = [(offset, min(packet_size, end - offset)) for offset in range(start, end, packet_size)]
            for (_, expected), block in zip(requests, reader.readv(requests, READ_REQUESTS), strict=True):
                check()
                if len(block) != expected:
                    raise OSError("The source file changed during transfer.")
                yield block


def copy_tree(job, source, source_path, dest, dest_path, overwrite, info=None, root=True):
    job.check()
    info = info or source.info(source_path)
    mode = info.st_mode
    existing = dest.info_or_none(dest_path)
    if stat.S_ISDIR(mode):
        if existing is not None:
            if not overwrite or not stat.S_ISDIR(existing.st_mode):
                raise FileExistsError(f"Destination already exists: {dest_path}")
        else:
            dest.mkdir(dest_path)
        for name, entry in source.entries(source_path, job.check):
            verify_name(name, windows=not dest.sftp and os.name == "nt")
            copy_tree(job, source, source.paths.join(source_path, name),
                      dest, dest.paths.join(dest_path, name), overwrite, entry, root=False)
        return
    if not stat.S_ISREG(mode):
        raise ValueError(f"Only regular files and folders can be transferred: {source_path}")
    if existing is not None and (not overwrite or not stat.S_ISREG(existing.st_mode)):
        raise FileExistsError(f"Destination already exists: {dest_path}")
    if root:
        job.update(total=info.st_size)
    temp = dest.paths.join(dest.paths.dirname(dest_path), f".xsync-{uuid.uuid4().hex}.part")
    created = False
    try:
        with source.open(source_path, "rb") as reader, dest.open(temp, "xb") as writer:
            created = True
            for chunk in stream_read(reader, source.sftp is not None, info.st_size, job.check):
                writer.write(chunk)
                job.progress(len(chunk))
        job.check()
        dest.publish(temp, dest_path, overwrite)
        created = False
    finally:
        if created:
            try:
                dest.remove(temp)
            except OSError:
                pass


def tools_for_job():
    return {
        "ssh": shutil.which("ssh"),
        "scp": shutil.which("scp"),
        "rsync": shutil.which(os.environ.get("XSYNC_RSYNC", "rsync")),
    }


def ssh_flags(host, pins=None):
    flags = [
        "-F", "none", "-p", str(host["port"]), "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={TIMEOUT}", "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2", "-o", "StrictHostKeyChecking=yes",
        "-o", "GlobalKnownHostsFile=none",
    ]
    if pins is not None:
        flags += ["-o", f'UserKnownHostsFile="{pins}"']
    if host["identity"]:
        flags += ["-i", host["identity"], "-o", "IdentitiesOnly=yes"]
    return flags


def run_process(job, args, timeout=None, report_progress=False, observe=None):
    job.check()
    tail = bytearray()
    no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                          creationflags=no_window) as process:
        def read_output():
            pending = b""
            while block := process.stdout.read1(4096):
                tail.extend(block)
                del tail[:-8192]
                if report_progress:
                    *lines, pending = re.split(rb"[\r\n]", pending + block)
                    pending = pending[-1024:]
                    for line in lines:
                        if match := RSYNC_PROGRESS.match(line):
                            count, percent, rate, unit, hour, minute, second = match.groups()
                            speed = float(rate) * 1024 ** [b"", b"k", b"M", b"G", b"T"].index(unit)
                            job.update(bytes=int(count.replace(b",", b"")), percent=int(percent),
                                       rate=speed, eta=int(hour) * 3600 + int(minute) * 60 + int(second))

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while process.poll() is None:
                job.check()
                if timeout is not None and time.monotonic() - started > timeout:
                    raise TimeoutError("SSH capability check timed out.")
                if observe:
                    observe()
                job.cancelled.wait(0.15)
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                                       capture_output=True, timeout=5, check=True, creationflags=no_window)
                    except (OSError, subprocess.SubprocessError):
                        process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
            reader.join()
    return process.returncode, tail.decode(errors="replace").strip()


def remote_command(job, tools, host, pins, command):
    return run_process(job, [tools["ssh"], *ssh_flags(host, pins), "-l", host["user"],
                             "--", host["host"], command], timeout=25)


def remote_spec(host, path):
    address = host["host"]
    if ":" in address:
        address = f"[{address}]"
    return f'{host["user"]}@{address}:{path}'


def local_rsync_path(path):
    if os.name != "nt":
        return path
    drive, rest = os.path.splitdrive(path)
    if not drive.endswith(":"):
        raise ValueError("Windows rsync requires a drive-letter path. Use SFTP for UNC shares.")
    root = "/cygdrive/" if os.environ.get("XSYNC_RSYNC_STYLE") != "msys" else "/"
    return root + drive[0].lower() + rest.replace("\\", "/")


def rsync_transport(args):
    if any("'" in item and '"' in item for item in args):
        raise ValueError("rsync cannot quote an SSH option containing both quote characters.")
    # rsync tokenizes -e itself, including on Windows.
    return " ".join("'" + item + "'" if '"' in item else '"' + item + '"' for item in args)


def rsync_leg(job, tools, host, pins, local, remote, upload):
    shell = rsync_transport([local_rsync_path(tools["ssh"]), *ssh_flags(host, pins)])
    if upload:
        source_kind(os.lstat(local).st_mode, local)
        reject_remote_target(job, tools, host, pins, remote)
        pair = [local_rsync_path(local), remote_spec(host, posixpath.dirname(remote).rstrip("/") + "/")]
    else:
        verify_remote_source(job, tools, host, pins, remote)
        reject_existing(local, job.overwrite)
        pair = [remote_spec(host, remote), local_rsync_path(os.path.dirname(local)).rstrip("/") + "/"]
    status, message = run_process(
        job, [tools["rsync"], "-rts", "--timeout=60", "--info=progress2", "--outbuf=L",
              *(["--ignore-existing"] if not job.overwrite else []), "-e", shell, "--", *pair],
        report_progress=True,
    )
    if status:
        raise RuntimeError(message or f"rsync exited with code {status}.")


def direct_rsync(job, tools, pins):
    source, dest = job.source, job.dest
    # The destination pin is public. Authentication uses only credentials on the source.
    key = dest["hostKey"]
    host_key = shlex.quote(f'{known_name(dest)} {key["type"]} {key["key"]}')
    flags = ssh_flags({**dest, "identity": job.direct_key})
    options = ' -o "UserKnownHostsFile=$pins/known_hosts"'
    preamble = (
        'set -eu; pins=$(mktemp -d /tmp/xsync-direct.XXXXXXXX); '
        'trap \'rm -rf -- "$pins"\' EXIT; trap \'exit 1\' HUP INT TERM; '
        f'printf "%s\\n" {host_key} > "$pins/known_hosts"; '
    )
    ssh_target = shlex.join(["-l", dest["user"], "--", dest["host"], "rsync --version"])
    probe = preamble + f"rsync --version >/dev/null && {shlex.join(['ssh', *flags])}{options} {ssh_target}"
    try:
        status, _ = remote_command(job, tools, source, pins[source["id"]], probe)
    except TimeoutError:
        return False
    if status:
        return False
    verify_remote_source(job, tools, source, pins[source["id"]], job.source_path)
    reject_remote_target(job, tools, dest, pins[dest["id"]], job.dest_path)
    target = remote_spec(dest, posixpath.dirname(job.dest_path).rstrip("/") + "/")
    pairs = shlex.join(["--", job.source_path, target])
    transport = shlex.quote(rsync_transport(["ssh", *flags]))
    copy_flags = "-rts --timeout=60 --info=progress2 --outbuf=L" + ("" if job.overwrite else " --ignore-existing")
    command = preamble + f'rsync {copy_flags} -e {transport}" -o UserKnownHostsFile=$pins/known_hosts" {pairs}'
    job.phase("rsync", "Transferring directly between servers")
    status, message = run_process(
        job, [tools["ssh"], *ssh_flags(source, pins[source["id"]]), "-l", source["user"],
              "--", source["host"], command], report_progress=True,
    )
    if status:
        raise RuntimeError(message or f"Direct rsync exited with code {status}.")
    return True


def reject_remote_target(job, tools, host, pins, path, file_only=False):
    quoted = shlex.quote(path)
    command = f"test -L {quoted}"
    if not job.overwrite:
        command += f" || test -e {quoted}"
    elif file_only:
        command += f" || (test -e {quoted} && test ! -f {quoted})"
    status, message = remote_command(job, tools, host, pins, command)
    if status not in (0, 1):
        raise RuntimeError(message or "Could not check the remote destination.")
    if status == 0:
        raise FileExistsError(f"Destination already exists: {path}")


def source_kind(mode, path, directory_allowed=True):
    if not (stat.S_ISREG(mode) or directory_allowed and stat.S_ISDIR(mode)):
        raise ValueError(f"Unsupported source type: {path}")


def verify_remote_source(job, tools, host, pins, path, directory_allowed=True, file_size=False):
    quoted = shlex.quote(path)
    types = f"test -f {quoted}"
    if directory_allowed:
        types += f" || test -d {quoted}"
    command = f"test ! -L {quoted} && ({types})"
    if file_size:
        command += f" && (stat -c %s {quoted} 2>/dev/null || stat -f %z {quoted} 2>/dev/null || true)"
    status, message = remote_command(job, tools, host, pins, command)
    if status:
        raise ValueError(message or f"Source is missing or has an unsupported type: {path}")
    if file_size and message.isdecimal():
        return int(message)


def reject_existing(path, overwrite):
    if os.path.lexists(path) and (not overwrite or os.path.islink(path)):
        raise FileExistsError(f"Destination already exists: {path}")


def scp_leg(job, tools, host, pins, local, remote, upload, legacy):
    if upload:
        info = os.lstat(local)
        source_kind(info.st_mode, local, directory_allowed=False)
        reject_remote_target(job, tools, host, pins, remote, file_only=True)
        size = info.st_size
    else:
        size = verify_remote_source(job, tools, host, pins, remote, directory_allowed=False, file_size=True)
        reject_existing(local, job.overwrite)
        if os.path.lexists(local):
            source_kind(os.lstat(local).st_mode, local, directory_allowed=False)
    if legacy:
        if any(ord(char) < 32 or ord(char) == 127 for char in remote):
            raise ValueError("Legacy SCP does not support control characters in paths.")
        # Legacy SCP uses a remote shell and matches the requested basename with fnmatch.
        # Backslash escaping keeps the name literal for both parsers.
        remote = re.sub(r"[^\w/.,+\-]", lambda char: "\\" + char[0], remote)
    remote = remote_spec(host, remote)
    flags = ssh_flags(host, pins)
    flags[2] = "-P"
    args = [tools["scp"], "-B", *(["-O"] if legacy else []), *flags, "--"]
    job.update(total=size)
    if upload:
        status, message = run_process(job, [*args, local, remote])
        if status:
            raise RuntimeError(message or f"SCP exited with code {status}.")
        job.progress(size)
        return
    # Publish the file only after SCP succeeds, so retrying a failed download is safe.
    with tempfile.TemporaryDirectory(prefix=".xsync-", dir=os.path.dirname(local)) as stage:
        staged_file = os.path.join(stage, os.path.basename(local))
        observed = 0

        def observe():
            nonlocal observed
            try:
                size = os.path.getsize(staged_file)
            except FileNotFoundError:
                return
            job.progress(max(0, size - observed))
            observed = size

        status, message = run_process(job, [*args, remote, staged_file], observe=observe)
        if status:
            raise RuntimeError(message or f"SCP exited with code {status}.")
        job.check()
        observe()
        Filesystem().publish(staged_file, local, job.overwrite)


def transfer(job):
    source, dest = job.source, job.dest
    hosts = [host for host in (source, dest) if host]
    tools = tools_for_job()
    mode = job.protocol
    if not hosts:
        if mode != "auto":
            raise ValueError("Select Auto for a local copy.")
        job.phase("Local", "Copying files")
        copy_tree(job, Filesystem(), job.source_path, Filesystem(), job.dest_path, job.overwrite)
        return

    with tempfile.TemporaryDirectory(prefix="xsync-") as workspace:
        pins = Path(workspace, "known_hosts")
        # Use a separate pin file per endpoint, including when profiles share an address.
        pin_files = {}
        for index, host in enumerate(hosts):
            pinned = Path(workspace, f"{pins.name}-{index}")
            key = host["hostKey"]
            pinned.write_text(f'{known_name(host)} {key["type"]} {key["key"]}\n', encoding="ascii")
            pin_files[host["id"]] = str(pinned).replace("\\", "/")

        job.phase("", "Checking available protocols")
        if job.direct and tools["ssh"] and direct_rsync(job, tools, pin_files):
            return
        rsync = False
        local_paths = [path for host, path in ((source, job.source_path), (dest, job.dest_path)) if not host]
        rsync_paths = os.name != "nt" or all(os.path.splitdrive(path)[0].endswith(":") for path in local_paths)
        if mode in {"auto", "rsync"} and tools["ssh"] and tools["rsync"] and rsync_paths:
            local_status, _ = run_process(job, [tools["rsync"], "--version"], timeout=10)
            if local_status == 0:
                rsync = True
                for host in hosts:
                    try:
                        status, _ = remote_command(job, tools, host, pin_files[host["id"]], "rsync --version")
                    except TimeoutError:
                        status = 255
                    rsync &= status == 0
        if mode == "rsync" and not rsync:
            raise RuntimeError("rsync is unavailable locally or on a remote endpoint, or SSH command access failed.")

        with ExitStack() as stack:
            endpoints = {}
            if not rsync and mode in {"auto", "sftp"}:
                for side, host in (("source", source), ("dest", dest)):
                    job.check()
                    try:
                        endpoints[side] = Filesystem(stack.enter_context(sftp_client(host, bulk=True)) if host else None)
                    except (OSError, EOFError, paramiko.SSHException) as exc:
                        if mode == "sftp" or isinstance(exc, paramiko.BadHostKeyException):
                            raise
                        if not tools["scp"] or not tools["ssh"]:
                            raise RuntimeError(f"SFTP failed and OpenSSH SCP is unavailable: {exc}") from exc
            if len(endpoints) == 2:
                left, right = endpoints["source"], endpoints["dest"]
                if source and dest and (source["host"], source["port"]) == (dest["host"], dest["port"]):
                    src = left.sftp.normalize(job.source_path)
                    dst = right.sftp.normalize(posixpath.dirname(job.dest_path))
                    if dst == src or dst.startswith(src.rstrip("/") + "/"):
                        raise ValueError("A folder cannot be copied into itself.")
                job.phase("SFTP", "Streaming files")
                copy_tree(job, left, job.source_path, right, job.dest_path, job.overwrite)
                return
            if not rsync and not all(tools[name] for name in ("scp", "ssh")):
                raise RuntimeError("OpenSSH scp and ssh are required for SCP.")
            if rsync:
                label = "rsync"
            else:
                label = " → ".join(
                    "SFTP" if side in endpoints else "SCP"
                    for side, host in (("source", source), ("dest", dest)) if host
                ).replace("SCP → SCP", "SCP")

            def leg(side, host, local, remote, upload, phase):
                pin = pin_files[host["id"]]
                job.phase(label, phase)
                if rsync:
                    rsync_leg(job, tools, host, pin, local, remote, upload)
                elif side in endpoints:
                    filesystem = endpoints[side]
                    if upload:
                        copy_tree(job, Filesystem(), local, filesystem, remote, job.overwrite)
                    else:
                        copy_tree(job, filesystem, remote, Filesystem(), local, False)
                else:
                    scp_leg(job, tools, host, pin, local, remote, upload, legacy=mode == "auto")

            if source and dest:
                incoming = Path(workspace, "incoming")
                incoming.mkdir()
                stage = str(incoming / verify_name(posixpath.basename(job.source_path)))
                leg("source", source, stage, job.source_path, False, "Downloading to temporary local storage")
                leg("dest", dest, stage, job.dest_path, True, "Uploading to destination")
            elif source:
                leg("source", source, job.dest_path, job.source_path, False, "Downloading")
            else:
                leg("dest", dest, job.source_path, job.dest_path, True, "Uploading")


class Job:
    def __init__(self, source, source_path, dest, dest_path, protocol, overwrite, workspace,
                 direct=False, direct_key=""):
        self.source = source
        self.dest = dest
        self.source_path = source_path
        self.dest_path = dest_path
        self.protocol = protocol
        self.overwrite = overwrite
        self.direct = direct
        self.direct_key = direct_key
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.sample_time = time.monotonic()
        self.phase_start = self.sample_time
        self.sample_bytes = 0
        self.data = {
            "id": uuid.uuid4().hex, "workspace": workspace, "state": "queued",
            "file": (posixpath if source else os.path).basename(source_path),
            "source": source["name"] if source else "This computer",
            "destination": dest["name"] if dest else "This computer",
            "protocol": "", "phase": "Waiting", "bytes": 0, "error": "",
            "total": None, "percent": None, "rate": 0, "eta": None,
        }

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def update(self, **data):
        with self.lock:
            self.data.update(data)

    def phase(self, protocol, phase):
        with self.lock:
            self.sample_time, self.sample_bytes = time.monotonic(), 0
            self.phase_start = self.sample_time
            self.data.update(protocol=protocol, phase=phase, bytes=0, total=None, percent=None, rate=0, eta=None)

    def progress(self, count):
        with self.lock:
            self.data["bytes"] += count
            now = time.monotonic()
            duration = now - self.sample_time
            if duration >= 0.35:
                self.data["rate"] = (self.data["bytes"] - self.sample_bytes) / duration
                self.sample_time, self.sample_bytes = now, self.data["bytes"]

    def complete(self):
        with self.lock:
            if not self.data["rate"] and self.data["bytes"]:
                self.data["rate"] = self.data["bytes"] / max(0.001, time.monotonic() - self.phase_start)
            self.data.update(state="done", phase="Completed", percent=100, eta=0)

    def clone(self):
        job = Job(self.source, self.source_path, self.dest, self.dest_path, self.protocol, self.overwrite,
                  self.data["workspace"], self.direct, self.direct_key)
        job.update(source=self.data["source"], destination=self.data["destination"])
        return job

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()


class TransferQueue:
    def __init__(self):
        self.jobs = OrderedDict()
        self.condition = threading.Condition()
        self.destinations = set()
        self.closed = False
        self.workers = [threading.Thread(target=self.work, daemon=True) for _ in range(2)]
        for worker in self.workers:
            worker.start()

    def add(self, job):
        return self.add_batch([job])[0]

    def add_batch(self, jobs):
        with self.condition:
            if self.closed:
                raise RuntimeError("The transfer queue is shutting down.")
            pending = sum(item.snapshot()["state"] in ACTIVE for item in self.jobs.values())
            if pending + len(jobs) > MAX_PENDING:
                raise ValueError("The transfer queue is full.")
            while len(self.jobs) + len(jobs) > MAX_HISTORY:
                oldest = next(key for key, item in self.jobs.items() if item.snapshot()["state"] not in ACTIVE)
                del self.jobs[oldest]
            records = []
            for job in jobs:
                record = job.snapshot()
                self.jobs[record["id"]] = job
                records.append(record)
            self.condition.notify_all()
            return records

    def list(self):
        with self.condition:
            return [job.snapshot() for job in reversed(self.jobs.values())]

    def cancel(self, job_id):
        with self.condition:
            job = self.jobs[job_id]
            job.cancelled.set()
            if job.snapshot()["state"] == "queued":
                job.update(state="cancelled", phase="Cancelled")

    def remove_completed(self, identifiers=None):
        with self.condition:
            for key in list(self.jobs) if identifiers is None else identifiers:
                item = self.jobs.get(key)
                if item and item.snapshot()["state"] not in ACTIVE:
                    del self.jobs[key]

    def retry(self, identifiers):
        with self.condition:
            candidates = [self.jobs[key].clone() for key in identifiers
                          if key in self.jobs and self.jobs[key].snapshot()["state"] in {"failed", "cancelled"}]
        return self.add_batch(candidates)

    def available(self):
        for job in self.jobs.values():
            if job.snapshot()["state"] == "queued":
                dest = (job.dest["host"], job.dest["port"]) if job.dest else None
                if dest not in self.destinations:
                    return job, dest
        return None

    def work(self):
        while True:
            with self.condition:
                while not self.closed and not (available := self.available()):
                    self.condition.wait()
                if self.closed:
                    return
                job, dest = available
                self.destinations.add(dest)
                job.update(state="running", phase="Starting")
            try:
                transfer(job)
                job.complete()
            except Cancelled:
                job.update(state="cancelled", phase="Cancelled")
            except Exception as exc:
                job.update(state="failed", phase="Failed", error=str(exc) or type(exc).__name__)
            finally:
                with self.condition:
                    self.destinations.remove(dest)
                    self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            for job in self.jobs.values():
                job.cancelled.set()
            self.condition.notify_all()
        for worker in self.workers:
            worker.join()

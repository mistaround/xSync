import getpass
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xsync.__main__ import Application, Favorites
from xsync.connections import scan_key
from xsync.filesystem import wsl_path, wsl_root


PAYLOAD = bytes(range(256)) * 1500


class LocalTransfers(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="xsync-test-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.app = Application(self.directory / "config")
        self.addCleanup(self.app.close)
        self.original = self.directory / "example file.txt"
        self.original.write_bytes(PAYLOAD)
        self.target = self.directory / "destination"
        self.target.mkdir()

    def submit(self, source=None, destination=None, mode="auto", path=None, target=None, overwrite=False,
               direct_key=None):
        return self.app.start_job({
            "source": source and source["id"], "destination": destination and destination["id"],
            "path": str(path or self.original), "directory": str(target or self.target),
            "protocol": mode, "workspace": "test", "overwrite": overwrite,
            "direct": direct_key is not None, "direct_key": str(direct_key) if direct_key else "",
        })

    def finished(self, job, state="done", protocol=None):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = next(item for item in self.app.jobs.list() if item["id"] == job["id"])
            if result["state"] not in {"queued", "running"}:
                self.assertEqual(result["state"], state, result)
                if protocol:
                    self.assertEqual(result["protocol"], protocol, result)
                return result
            time.sleep(0.03)
        self.fail(f"Transfer timed out: {result}")


class LocalTransferTests(LocalTransfers):
    def test_streaming_copy_and_overwrite(self):
        resulting_file = self.target / self.original.name
        self.finished(self.submit(), protocol="Local")
        self.assertEqual(resulting_file.read_bytes(), PAYLOAD)
        self.original.write_bytes(b"new")
        self.finished(self.submit(), state="failed")
        self.assertEqual(resulting_file.read_bytes(), PAYLOAD)
        self.finished(self.submit(overwrite=True))
        self.assertEqual(resulting_file.read_bytes(), b"new")
        self.assertEqual(list(self.target.iterdir()), [resulting_file])

    def test_copy_into_self_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the source"):
            self.submit(path=self.directory, target=self.target)

    def test_favorites_persist_per_endpoint(self):
        local, remote = str(self.directory), "/srv/data"
        self.app.favorites.change("", local)
        self.app.favorites.change("ssh-profile", remote)
        self.app.favorites.change("ssh-profile", remote)
        self.app.favorites.change("wsl:Ubuntu", wsl_root("wsl:Ubuntu"))
        restored = Favorites(self.directory / "config")
        self.assertEqual(restored.list(""), [local])
        self.assertEqual(restored.list("ssh-profile"), [remote])
        self.assertEqual(restored.list("wsl:Ubuntu"), [wsl_root("wsl:Ubuntu")])
        restored.change("ssh-profile", remote, remove=True)
        self.assertEqual(restored.list("ssh-profile"), [])
        self.assertEqual(restored.list(""), [local])

    def test_wsl_share_is_scoped_to_selected_distribution(self):
        root = wsl_root("wsl:Ubuntu")
        self.assertEqual(wsl_path("wsl:Ubuntu"), root)
        self.assertEqual(wsl_path("wsl:Ubuntu", root + r"home\dev"), root + r"home\dev")
        self.assertEqual(wsl_path("wsl:Ubuntu", "/home/dev"), root + r"home\dev")
        self.assertEqual(wsl_path("wsl:Ubuntu", r"\\wsl$\Ubuntu\home"), r"\\wsl$\Ubuntu\home")
        with self.assertRaises(ValueError):
            wsl_path("wsl:Ubuntu", wsl_root("wsl:Debian"))

    def test_operations_and_retry(self):
        self.app.file_operation("", "mkdir", [str(self.target)], "nested")
        folder = self.target / "nested"
        self.assertTrue(folder.is_dir())
        self.app.file_operation("", "rename", [str(folder)], "renamed")
        renamed = self.target / "renamed"
        self.assertTrue(renamed.is_dir())
        self.app.file_operation("", "delete", [str(renamed)])
        self.assertFalse(renamed.exists())
        missing = self.original.with_name("missing.txt")
        job = self.finished(self.submit(path=missing), state="failed")
        missing.write_bytes(b"retry")
        retry, = self.app.jobs.retry([job["id"]])
        finished = self.finished(retry)
        self.assertEqual(finished["percent"], 100)
        self.assertEqual(finished["total"], len(b"retry"))
        self.assertEqual((self.target / missing.name).read_bytes(), b"retry")
        self.app.jobs.remove_completed([job["id"]])
        self.assertEqual([item["id"] for item in self.app.jobs.list()], [retry["id"]])


@unittest.skipUnless(os.name == "posix" and shutil.which("ssh-keygen") and shutil.which("sshd"),
                     "An OpenSSH server and ssh-keygen are needed for loopback integration tests.")
class SSHTransferTests(LocalTransfers):
    def key(self, file):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(file)], check=True)

    def setUp(self):
        super().setUp()
        self.client_key = self.directory / "client_key"
        self.key(self.client_key)

    def server(self, name, sftp=True):
        root = self.directory / name
        root.mkdir()
        host_key = root / "host"
        self.key(host_key)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = root / "sshd_config"
        config.write_text(
            f"HostKey {host_key}\nPort {port}\nListenAddress 127.0.0.1\n"
            f"AuthorizedKeysFile {self.client_key}.pub\nStrictModes no\nUsePAM no\n"
            f"PidFile {root / 'pid'}\n"
            + ("Subsystem sftp internal-sftp\n" if sftp else "")
            + f"SetEnv PATH={os.environ['PATH']}\n",
            encoding="utf-8",
        )
        with (root / "server.log").open("wb") as log:
            daemon = subprocess.Popen([shutil.which("sshd"), "-D", "-e", "-f", str(config)], stderr=log)

        def stop():
            if daemon.poll() is None:
                daemon.terminate()
            daemon.wait(timeout=5)

        self.addCleanup(stop)
        for _ in range(50):
            if daemon.poll() is not None:
                self.skipTest((root / "server.log").read_text())
            try:
                key = scan_key("127.0.0.1", port)
                break
            except ConnectionRefusedError:
                time.sleep(0.03)
        else:
            self.fail("The temporary SSH server did not accept connections.")
        return self.app.profiles.save({
            "name": name, "host": "127.0.0.1", "port": port, "user": getpass.getuser(),
            "identity": str(self.client_key), "hostKey": key,
        })

    def test_sftp_auto_upload_and_remote_relay(self):
        first = self.server("first")
        second = self.server("second")
        folder = self.directory / "example folder"
        folder.mkdir()
        (folder / "multiple blocks.bin").write_bytes(PAYLOAD)
        (folder / "nested").mkdir()
        (folder / "nested" / "empty.txt").touch()
        remote_target = self.directory / "first" / folder.name
        self.finished(self.submit(destination=first, path=folder, target=remote_target.parent, mode="sftp"),
                      protocol="SFTP")
        self.assertEqual((remote_target / "multiple blocks.bin").read_bytes(), PAYLOAD)
        with patch.dict(os.environ, {"XSYNC_RSYNC": "xsync-unavailable-rsync"}):
            self.finished(self.submit(source=first, destination=second, path=remote_target,
                                      target=self.directory / "second"), protocol="SFTP")
        result = self.directory / "second" / folder.name
        self.assertEqual((result / "multiple blocks.bin").read_bytes(), PAYLOAD)
        self.assertEqual((result / "nested" / "empty.txt").stat().st_size, 0)
        listing = self.app.request("/api/list", {"host": second["id"], "path": str(result)})
        self.assertEqual([item["name"] for item in listing["entries"]], ["nested", "multiple blocks.bin"])

    def test_browse_reuses_connection_and_refreshes_after_remote_operations(self):
        peer = self.server("peer")
        root = self.directory / "peer"
        before = self.app.browse(peer["id"], str(root))
        self.app.file_operation(peer["id"], "mkdir", [str(root)], "new")
        self.app.file_operation(peer["id"], "rename", [str(root / "new")], "renamed")
        self.assertIs(self.app.browse(peer["id"], str(root)), before)
        session = next(iter(self.app.browser.entries.values())).sftp
        after = self.app.browse(peer["id"], str(root), force=True)
        self.assertIn("renamed", [item["name"] for item in after["entries"]])
        self.assertIs(next(iter(self.app.browser.entries.values())).sftp, session)
        self.assertEqual(len(session._expecting), 0)
        (root / "renamed" / "file.txt").write_text("remove me", encoding="utf-8")
        self.app.file_operation(peer["id"], "delete", [str(root / "renamed")])
        self.assertFalse((root / "renamed").exists())

    def test_prefetched_download_spans_multiple_windows(self):
        peer = self.server("peer")
        file = self.directory / "peer" / "large file.dat"
        payload = PAYLOAD * 15 + b"tail"
        file.write_bytes(payload)
        job = self.finished(self.submit(source=peer, path=file, mode="sftp"), protocol="SFTP")
        self.assertEqual(job["total"], len(payload))
        self.assertEqual(job["bytes"], len(payload))
        self.assertEqual((self.target / file.name).read_bytes(), payload)

    def test_scp_with_legacy_and_mixed_fallback(self):
        legacy = self.server("legacy", sftp=False)
        modern = self.server("modern")
        self.original = self.original.rename(self.directory / "file's name.txt")
        with patch.dict(os.environ, {"XSYNC_RSYNC": "xsync-unavailable-rsync"}):
            self.finished(self.submit(destination=legacy, target=self.directory / "legacy"), protocol="SCP")
            self.finished(self.submit(source=legacy, destination=modern,
                                      path=self.directory / "legacy" / self.original.name,
                                      target=self.directory / "modern"), protocol="SCP → SFTP")
        self.assertEqual((self.directory / "modern" / self.original.name).read_bytes(), PAYLOAD)
        self.finished(self.submit(source=modern, path=self.directory / "modern" / self.original.name,
                                  mode="scp"), protocol="SCP")
        self.assertEqual((self.target / self.original.name).read_bytes(), PAYLOAD)

    @unittest.skipUnless(shutil.which("rsync"), "rsync is not in PATH.")
    def test_auto_prefers_rsync_for_two_remote_servers(self):
        first = self.server("first")
        second = self.server("second")
        original = self.directory / "first" / self.original.name
        original.write_bytes(PAYLOAD)
        self.finished(self.submit(source=first, destination=second, path=original,
                                  target=self.directory / "second"), protocol="rsync")
        self.assertEqual((self.directory / "second" / self.original.name).read_bytes(), PAYLOAD)

    @unittest.skipUnless(shutil.which("rsync"), "rsync is not in PATH.")
    def test_direct_rsync_without_local_rsync(self):
        first = self.server("first")
        second = self.server("second")
        original = self.directory / "first" / self.original.name
        original.write_bytes(PAYLOAD)
        with patch.dict(os.environ, {"XSYNC_RSYNC": "xsync-unavailable-rsync"}):
            self.finished(self.submit(source=first, destination=second, path=original,
                                      target=self.directory / "second", direct_key=self.client_key),
                          protocol="rsync")
        self.assertEqual((self.directory / "second" / self.original.name).read_bytes(), PAYLOAD)


if __name__ == "__main__":
    unittest.main()

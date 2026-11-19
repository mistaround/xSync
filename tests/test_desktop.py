import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None
else:
    from xsync.desktop import Session


@unittest.skipUnless(QApplication, "Install requirements-qt.txt for desktop tests.")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform.startswith("linux") and "DISPLAY" not in os.environ:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="xsync-desktop-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.target = self.root / "destination"
        self.target.mkdir()
        self.first = self.source / "文件 one.txt"
        self.second = self.source / "file two.txt"
        for file in (self.first, self.second):
            file.write_text(file.name, encoding="utf-8")
        self.session = Session(self.root / "config")
        self.addCleanup(self.stop_session)
        self.window = self.session.new_window((("", str(self.source)), ("", str(self.target))))
        self.tab = self.window.tabs.currentWidget()
        self.wait_for(lambda: self.tab.left.view.topLevelItemCount() == 2 and not self.tab.right.updating)

    def stop_session(self):
        QApplication.clipboard().clear()
        self.session.pool.waitForDone()
        self.session.app.jobs.close()
        for window in list(self.session.windows):
            window.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait_for(self, check):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if check():
                return
            QTest.qWait(10)
        self.fail("Qt operation timed out")

    def expect_file(self, path, source):
        self.wait_for(path.is_file)
        self.assertEqual(path.read_bytes(), source.read_bytes())

    def paste(self, view):
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    def test_paste_external_file_clipboard(self):
        clipboard = QMimeData()
        clipboard.setUrls([QUrl.fromLocalFile(str(self.first)), QUrl.fromLocalFile(str(self.second))])
        QApplication.clipboard().setMimeData(clipboard)
        self.paste(self.tab.right.view)
        self.expect_file(self.target / self.first.name, self.first)
        self.expect_file(self.target / self.second.name, self.second)
        self.assertEqual(len(self.session.app.jobs.list()), 2)

    def test_copy_from_local_pane_and_paste_in_second_window(self):
        listing = self.tab.left.view
        listing.topLevelItem(0).setSelected(True)
        selected, = listing.paths()
        QTest.keyClick(listing, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        clipboard = QApplication.clipboard().mimeData()
        self.assertEqual([Path(url.toLocalFile()) for url in clipboard.urls()], [Path(selected)])
        sibling = self.session.new_window(self.tab.snapshot())
        self.paste(sibling.tabs.currentWidget().right.view)
        self.expect_file(self.target / Path(selected).name, Path(selected))
        self.wait_for(lambda: sibling.jobs.topLevelItemCount() == self.window.jobs.topLevelItemCount() == 1)

    def test_drop_external_file_on_subfolder(self):
        subfolder = self.target / "nested"
        subfolder.mkdir()
        self.tab.right.reload()
        view = self.tab.right.view
        self.wait_for(lambda: view.topLevelItemCount() == 1)
        where = view.visualItemRect(view.topLevelItem(0)).center()
        clipboard = QMimeData()
        clipboard.setUrls([QUrl.fromLocalFile(str(self.first))])
        enter = QDragEnterEvent(where, Qt.DropAction.CopyAction, clipboard,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), enter)
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(QPointF(where), Qt.DropAction.CopyAction, clipboard,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), drop)
        self.assertTrue(drop.isAccepted())
        self.expect_file(subfolder / self.first.name, self.first)

    def test_internal_remote_payload_is_scoped_to_session(self):
        mime = self.session.mime("stashed-host-id", ["/data/a.txt", "/data/b.txt"])
        self.assertEqual(self.session.files_from(mime), ("stashed-host-id", ["/data/a.txt", "/data/b.txt"]))
        with patch.object(self.session, "drag_token", "foreign-session"):
            with self.assertRaisesRegex(ValueError, "仅在当前"):
                self.session.files_from(mime)


if __name__ == "__main__":
    unittest.main()

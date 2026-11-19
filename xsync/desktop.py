import argparse
import json
import posixpath
import secrets
import uuid
from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDrag, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QSplitter, QStyle, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .__main__ import Application, default_data_dir
from .transfers import ACTIVE, MAX_PENDING


FILE_MIME = "application/vnd.xsync.files+json"
ROLE = Qt.ItemDataRole.UserRole
PHASES = {
    "Waiting": "等待中", "Starting": "正在启动", "Checking available protocols": "正在检测传输协议",
    "Copying files": "正在复制", "Streaming files": "正在传输",
    "Transferring directly between servers": "正在远端直传",
    "Downloading to temporary local storage": "正在下载到本机暂存", "Uploading to destination": "正在上传到目标",
    "Uploading": "正在上传", "Downloading": "正在下载", "Completed": "已完成",
    "Failed": "失败", "Cancelled": "已取消",
}
STYLE = """
QMainWindow, QDialog { background: #f0f5f2; color: #1b3229; }
QWidget { font-size: 13px; }
QLabel#title { font-size: 21px; font-weight: bold; color: #173f31; }
QLabel#heading { font-weight: bold; color: #254c3d; }
QLabel#status { color: #50675d; }
QLineEdit, QComboBox, QSpinBox { background: white; padding: 5px; border: 1px solid #cfddd6; border-radius: 5px; }
QListWidget, QTreeWidget { background: white; alternate-background-color: #f7faf8;
    border: 1px solid #d7e3db; border-radius: 5px; }
QTreeWidget::item { padding: 3px 0; }
QHeaderView::section { background: #eaf2ed; border: 0; border-bottom: 1px solid #d7e3db; padding: 6px; }
QPushButton { background: white; border: 1px solid #cbdad1; border-radius: 5px; padding: 5px 10px; }
QPushButton:hover { background: #e4f1e9; }
QPushButton:disabled { color: #7a8b84; background: #f4f7f5; }
QPushButton#primary { color: white; background: #24624c; border-color: #24624c; font-weight: bold; }
QPushButton#primary:hover { background: #194c3a; }
"""


def control(text, action, parent):
    button = QPushButton(text, parent)
    button.clicked.connect(action)
    return button


def size_text(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


class Results(QObject):
    finished = Signal(object, str)


class Work(QRunnable):
    def __init__(self, action, receiver):
        super().__init__()
        self.action = action
        self.results = Results()
        self.results.finished.connect(receiver, Qt.ConnectionType.QueuedConnection)

    def run(self):
        try:
            result = self.action()
        except Exception as error:
            self.results.finished.emit(None, str(error) or type(error).__name__)
        else:
            self.results.finished.emit(result, "")


class Session(QObject):
    changed = Signal()

    def __init__(self, data_dir):
        super().__init__()
        self.app = Application(data_dir)
        self.drag_token = secrets.token_urlsafe(24)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.windows = set()

    def new_window(self, initial=None):
        window = MainWindow(self, initial)
        self.windows.add(window)
        window.show()
        return window

    def mime(self, host, paths):
        data = QMimeData()
        if host:
            payload = json.dumps({"session": self.drag_token, "host": host, "paths": paths})
            data.setData(FILE_MIME, QByteArray(payload.encode()))
        else:
            data.setUrls([QUrl.fromLocalFile(path) for path in paths])
        return data

    def files_from(self, data):
        if data.hasFormat(FILE_MIME):
            encoded = data.data(FILE_MIME)
            if encoded.size() > 300_000:
                raise ValueError("剪贴板中的文件列表过大。")
            try:
                payload = json.loads(bytes(encoded))
                host, paths = payload["host"], payload["paths"]
                if payload["session"] != self.drag_token or not isinstance(host, str) or not host:
                    raise ValueError
                if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                    raise ValueError
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("远端文件的剪贴板内容仅在当前 xSync 会话中可用。") from exc
            return host, paths
        return "", [url.toLocalFile() for url in data.urls() if url.isLocalFile()]

    def enqueue(self, host, paths, target, scope, **options):
        paths = list(dict.fromkeys(paths))
        if not paths:
            raise ValueError("没有可以传输的文件。")
        if len(paths) > MAX_PENDING:
            raise ValueError(f"一次最多可加入 {MAX_PENDING} 个任务。可以选择整个文件夹。")
        options.setdefault("directory", target.path)
        jobs = [
            self.app.create_job({
                "source": host, "destination": target.host, "path": path,
                "workspace": scope, **options,
            })
            for path in paths
        ]
        self.app.jobs.add_batch(jobs)
        return len(jobs)


class FileView(QTreeWidget):
    def __init__(self, pane):
        super().__init__(pane)
        self.pane = pane
        self.setObjectName("files")
        self.setHeaderLabels(["名称", "大小"])
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.itemActivated.connect(pane.open_item)

    def paths(self):
        return [item.data(0, ROLE)["path"] for item in self.selectedItems()
                if item.data(0, ROLE)["kind"] in {"file", "directory"}]

    def startDrag(self, _supported_actions):
        paths = self.paths()
        if paths:
            drag = QDrag(self)
            drag.setMimeData(self.pane.session.mime(self.pane.host, paths))
            drag.exec(Qt.DropAction.CopyAction)
            drag.deleteLater()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            paths = self.paths()
            if paths:
                QApplication.clipboard().setMimeData(self.pane.session.mime(self.pane.host, paths))
            event.accept()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.pane.receive(QApplication.clipboard().mimeData())
            event.accept()
        else:
            super().keyPressEvent(event)

    def accept_file_event(self, event):
        mime = event.mimeData()
        if self.pane.host is not None and self.pane.path and (mime.hasUrls() or mime.hasFormat(FILE_MIME)):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragEnterEvent(self, event):
        self.accept_file_event(event)

    def dragMoveEvent(self, event):
        self.accept_file_event(event)

    def dropEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        entry = item.data(0, ROLE) if item else None
        directory = entry["path"] if entry and entry["kind"] == "directory" else self.pane.path
        if self.pane.receive(event.mimeData(), directory):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class FilePane(QWidget):
    def __init__(self, workspace, endpoint, path):
        super().__init__(workspace)
        self.workspace = workspace
        self.session = workspace.session
        self.host = endpoint
        self.path = path
        self.parent_path = ""
        self.request = None
        self.pending = None
        self.updating = False

        self.endpoint = QComboBox(self)
        self.endpoint.setObjectName("endpoint")
        self.endpoint.currentIndexChanged.connect(self.select_endpoint)
        self.address = QLineEdit(self)
        self.address.setObjectName("address")
        self.address.setPlaceholderText("输入绝对路径后按 Enter")
        self.address.returnPressed.connect(self.reload)
        self.up = control("↑", lambda: self.load(self.parent_path), self)
        self.up.setToolTip("返回上级目录")
        self.refresh = control("刷新", self.reload, self)
        self.status = QLabel(self)
        self.status.setObjectName("status")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.view = FileView(self)

        nav = QHBoxLayout()
        nav.addWidget(self.up)
        nav.addWidget(self.address, 1)
        nav.addWidget(self.refresh)
        layout = QVBoxLayout(self)
        layout.addWidget(self.endpoint)
        layout.addLayout(nav)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.status)
        self.session.changed.connect(self.refresh_endpoints)
        self.refresh_endpoints()
        self.load(self.path)

    @Slot()
    def refresh_endpoints(self):
        hosts = self.session.app.profiles.list()
        current = self.host
        self.endpoint.blockSignals(True)
        self.endpoint.clear()
        self.endpoint.addItem("选择连接", None)
        self.endpoint.addItem("此电脑", "")
        for host in hosts:
            self.endpoint.addItem(host["name"], host["id"])
        index = self.endpoint.findData(current)
        self.endpoint.setCurrentIndex(max(0, index))
        self.endpoint.blockSignals(False)
        if index < 0 and current is not None:
            self.set_endpoint(None)

    @Slot()
    def select_endpoint(self):
        self.set_endpoint(self.endpoint.currentData())

    def set_endpoint(self, host):
        if host == self.host:
            return
        self.host = host
        self.endpoint.blockSignals(True)
        self.endpoint.setCurrentIndex(max(0, self.endpoint.findData(host)))
        self.endpoint.blockSignals(False)
        self.load("")

    def load(self, path):
        self.path = path
        self.address.setText(path)
        self.parent_path = ""
        self.view.clear()
        self.up.setEnabled(False)
        self.address.setEnabled(self.host is not None)
        self.refresh.setEnabled(self.host is not None)
        if self.host is None:
            self.pending = None
            self.status.setText("从列表选择一个连接，或打开本机目录。")
            return
        self.status.setText("正在打开目录…")
        self.pending = (self.host, path)
        self.start_pending()

    def start_pending(self):
        if self.updating or self.pending is None:
            return
        self.request = self.pending
        self.pending = None
        self.updating = True
        host, path = self.request
        app = self.session.app
        self.session.pool.start(Work(lambda: app.request("/api/list", {"host": host, "path": path}), self.listed))

    @Slot(object, str)
    def listed(self, listing, error):
        self.updating = False
        if self.request != (self.host, self.path) or self.pending is not None:
            self.start_pending()
            return
        if error:
            self.status.setText(f"无法浏览目录。{error}")
            return
        self.path = listing["path"]
        self.parent_path = listing["parent"]
        if not self.address.hasFocus() or self.address.text() == self.request[1]:
            self.address.setText(self.path)
        self.up.setEnabled(self.parent_path != self.path)
        icons = {
            "directory": self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon),
            "file": self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon),
        }
        items = []
        for entry in listing["entries"]:
            item = QTreeWidgetItem([entry["name"], size_text(entry["size"]) if entry["kind"] == "file" else ""])
            item.setData(0, ROLE, entry)
            if entry["kind"] in icons:
                item.setIcon(0, icons[entry["kind"]])
            items.append(item)
        self.view.addTopLevelItems(items)
        cap = "（已达到显示上限）" if listing["truncated"] else ""
        self.status.setText(f"{len(items)} 项{cap}")

    @Slot()
    def reload(self):
        self.load(self.address.text())

    def open_item(self, item, _column):
        entry = item.data(0, ROLE)
        if entry["kind"] == "directory":
            self.load(entry["path"])

    def receive(self, data, directory=None):
        if self.host is None or not (directory or self.path):
            self.workspace.window().statusBar().showMessage("请先打开目标目录。", 5000)
            return False
        if data is None:
            return False
        try:
            source, paths = self.session.files_from(data)
            count = self.session.enqueue(source, paths, self, self.workspace.id, directory=directory or self.path)
        except (ValueError, OSError, KeyError, RuntimeError) as error:
            self.workspace.window().statusBar().showMessage(str(error), 6000)
            return False
        self.workspace.window().statusBar().showMessage(f"已添加 {count} 个传输任务。", 3500)
        return True


class TransferDialog(QDialog):
    def __init__(self, origin, target, parent):
        super().__init__(parent)
        self.setWindowTitle("新建传输")
        self.setMinimumWidth(450)
        self.origin, self.target = origin, target
        self.paths = origin.view.paths()
        self.source = QLineEdit(self)
        if len(self.paths) > 1:
            self.source.setText(f"已选择 {len(self.paths)} 项")
            self.source.setReadOnly(True)
        elif self.paths:
            self.source.setText(self.paths[0])
        self.dest = QLineEdit(target.path, self)
        self.protocol = QComboBox(self)
        for title, protocol in [("自动 · rsync → SFTP → SCP", "auto"), ("rsync", "rsync"),
                                ("SFTP", "sftp"), ("SCP", "scp")]:
            self.protocol.addItem(title, protocol)
        self.overwrite = QCheckBox("覆盖同名文件并合并文件夹", self)
        self.direct = QCheckBox("尝试远端直传 rsync", self)
        self.direct_key = QLineEdit(self)
        self.direct_key.setPlaceholderText("源主机上的私钥路径（可选）")
        self.error = QLabel(self)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #a03838")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("加入队列")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.enqueue)
        self.buttons.rejected.connect(self.reject)
        self.direct.toggled.connect(self.update_direct)
        self.protocol.currentIndexChanged.connect(self.update_direct)
        form = QFormLayout(self)
        form.addRow("来源路径", self.source)
        form.addRow("目标文件夹", self.dest)
        form.addRow("协议", self.protocol)
        form.addRow(self.overwrite)
        form.addRow(self.direct)
        form.addRow(self.direct_key)
        form.addRow(self.error)
        form.addRow(self.buttons)
        self.update_direct()

    @Slot()
    def update_direct(self):
        allowed = bool(self.origin.host and self.target.host and self.protocol.currentData() in {"auto", "rsync"})
        self.direct.setVisible(allowed)
        self.direct_key.setVisible(allowed and self.direct.isChecked())

    @Slot()
    def enqueue(self):
        paths = self.paths if len(self.paths) > 1 else [self.source.text()]
        try:
            self.origin.session.enqueue(
                self.origin.host, paths, self.target, self.origin.workspace.id,
                directory=self.dest.text(), protocol=self.protocol.currentData(),
                overwrite=self.overwrite.isChecked(), direct=self.direct.isVisible() and self.direct.isChecked(),
                direct_key=self.direct_key.text(),
            )
        except (ValueError, OSError, KeyError, RuntimeError) as error:
            self.error.setText(str(error))
        else:
            self.accept()


class HostDialog(QDialog):
    def __init__(self, session, current, parent):
        super().__init__(parent)
        current = current or {}
        self.session = session
        self.identifier = current.get("id")
        self.host_key = current.get("hostKey")
        self.address_at_scan = None
        self.setMinimumWidth(460)
        self.setWindowTitle("编辑连接" if self.identifier else "添加连接")
        self.name = QLineEdit(current.get("name", ""), self)
        self.host = QLineEdit(current.get("host", ""), self)
        self.port = QSpinBox(self)
        self.port.setRange(1, 65535)
        self.port.setValue(current.get("port", 22))
        self.username = QLineEdit(current.get("user", ""), self)
        self.identity = QLineEdit(current.get("identity", ""), self)
        identity = QHBoxLayout()
        identity.addWidget(self.identity)
        identity.addWidget(control("浏览…", self.pick_key, self))
        self.scan = control("获取主机指纹", self.scan_host, self)
        self.fingerprint = QLabel(self.host_key["fingerprint"] if self.host_key else "", self)
        self.fingerprint.setWordWrap(True)
        self.fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.trust = QCheckBox("我已与管理员核对指纹", self)
        self.trust.setChecked(bool(self.host_key))
        self.error = QLabel(self)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #a03838")
        self.saved = None
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save_host)
        buttons.rejected.connect(self.reject)
        self.host.textChanged.connect(self.invalidate)
        self.port.valueChanged.connect(self.invalidate)
        form = QFormLayout(self)
        form.addRow("名称", self.name)
        form.addRow("主机名或 IP", self.host)
        form.addRow("端口", self.port)
        form.addRow("SSH 用户名", self.username)
        form.addRow("私钥路径（可选）", identity)
        form.addRow(self.scan)
        form.addRow(self.fingerprint)
        form.addRow(self.trust)
        form.addRow(self.error)
        form.addRow(buttons)

    @Slot()
    def pick_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 SSH 私钥", self.identity.text())
        if path:
            self.identity.setText(path)

    def invalidate(self):
        self.host_key = None
        self.fingerprint.clear()
        self.trust.setChecked(False)

    @Slot()
    def scan_host(self):
        self.invalidate()
        self.error.clear()
        self.scan.setEnabled(False)
        self.address_at_scan = self.host.text(), self.port.value()
        host, port = self.address_at_scan
        app = self.session.app
        self.session.pool.start(Work(lambda: app.request("/api/scan", {"host": host, "port": port}), self.scanned))

    @Slot(object, str)
    def scanned(self, key, error):
        self.scan.setEnabled(True)
        if self.address_at_scan != (self.host.text(), self.port.value()):
            return
        if error:
            self.error.setText(error)
        else:
            self.host_key = key
            self.fingerprint.setText(key["fingerprint"])

    @Slot()
    def save_host(self):
        if not self.host_key or not self.trust.isChecked():
            self.error.setText("请获取并核对主机指纹。")
            return
        try:
            self.saved = self.session.app.profiles.save({
                "id": self.identifier, "name": self.name.text(), "host": self.host.text(),
                "port": self.port.value(), "user": self.username.text(), "identity": self.identity.text(),
                "hostKey": self.host_key,
            })
        except Exception as error:
            self.error.setText(str(error))
            return
        self.session.changed.emit()
        self.accept()


class Workspace(QWidget):
    def __init__(self, session, initial, parent):
        super().__init__(parent)
        self.id = uuid.uuid4().hex
        self.session = session
        self.left = FilePane(self, *initial[0])
        self.right = FilePane(self, *initial[1])
        buttons = QWidget(self)
        arrows = QVBoxLayout(buttons)
        arrows.addStretch()
        upload = control("→", lambda: self.transfer(self.left, self.right), self)
        upload.setToolTip("从左侧传输到右侧")
        upload.setObjectName("primary")
        arrows.addWidget(upload)
        download = control("←", lambda: self.transfer(self.right, self.left), self)
        download.setToolTip("从右侧传输到左侧")
        arrows.addWidget(download)
        arrows.addStretch()
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.left)
        splitter.addWidget(buttons)
        splitter.addWidget(self.right)
        for index in range(3):
            splitter.setCollapsible(index, False)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(2, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def transfer(self, origin, target):
        if origin.host is None or target.host is None:
            self.window().statusBar().showMessage("请先为左右两栏选择端点。", 5000)
            return
        dialog = TransferDialog(origin, target, self)
        dialog.exec()
        dialog.deleteLater()

    def snapshot(self):
        return ((self.left.host, self.left.path), (self.right.host, self.right.path))


class MainWindow(QMainWindow):
    def __init__(self, session, initial):
        super().__init__()
        self.session = session
        self.setWindowTitle("xSync · 文件传输")
        self.resize(1160, 740)
        self.setMinimumSize(800, 525)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.tabs = QTabWidget(self)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tab_number = 0
        self.add_tab(initial)

        self.hosts = QListWidget(self)
        self.hosts.itemClicked.connect(self.connect_host)
        self.hosts.itemDoubleClicked.connect(lambda _item: self.edit_host())
        self.session.changed.connect(self.refresh_hosts)
        self.refresh_hosts()
        self.jobs = QTreeWidget(self)
        self.jobs.setHeaderLabels(["文件", "路径", "协议", "状态"])
        self.jobs.setRootIsDecorated(False)
        self.jobs.setAlternatingRowColors(True)
        for column in (1, 2):
            self.jobs.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.jobs.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.jobs.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.jobs.setMinimumHeight(110)
        self.jobs.setMaximumHeight(194)
        self.last_jobs = None

        sidebar = QWidget(self)
        sidebar.setMaximumWidth(245)
        sidebar.setMinimumWidth(135)
        side = QVBoxLayout(sidebar)
        connection_heading = QLabel("已保存的连接", self)
        connection_heading.setObjectName("heading")
        side.addWidget(connection_heading)
        side.addWidget(self.hosts, 1)
        side.addWidget(control("添加连接", self.add_host, self))
        side.addWidget(control("编辑所选连接", self.edit_host, self))
        side.addWidget(control("移除所选连接", self.remove_host, self))

        central = QWidget(self)
        column = QVBoxLayout(central)
        column.setContentsMargins(16, 12, 16, 5)
        title = QLabel("xSync", self)
        title.setObjectName("title")
        toolbar = QHBoxLayout()
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(control("新建标签页", self.add_tab, self))
        toolbar.addWidget(control("新窗口", self.open_window, self))
        column.addLayout(toolbar)
        body = QHBoxLayout()
        body.addWidget(sidebar, 1)
        body.addWidget(self.tabs, 5)
        column.addLayout(body, 1)
        queue_bar = QHBoxLayout()
        queue_title = QLabel("传输队列", self)
        queue_title.setObjectName("heading")
        queue_bar.addWidget(queue_title)
        queue_bar.addStretch()
        queue_bar.addWidget(control("取消选中任务", self.cancel, self))
        column.addLayout(queue_bar)
        column.addWidget(self.jobs)
        self.setCentralWidget(central)
        self.statusBar().showMessage("可将文件拖到目标面板，或在目标面板按 Ctrl+V。")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_jobs)
        self.timer.start(600)
        self.refresh_jobs()

    @Slot()
    def add_tab(self, initial=None):
        if self.tabs.count() >= 16:
            self.statusBar().showMessage("请先关闭一个工作区。", 4000)
            return
        self.tab_number += 1
        tab = Workspace(self.session, initial or (("", ""), (None, "")), self.tabs)
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, f"工作区 {self.tab_number}"))

    @Slot(int)
    def close_tab(self, index):
        tab = self.tabs.widget(index)
        self.tabs.removeTab(index)
        tab.deleteLater()
        if not self.tabs.count():
            self.add_tab()

    @Slot()
    def open_window(self):
        self.session.new_window(self.tabs.currentWidget().snapshot())

    @Slot()
    def refresh_hosts(self):
        current = self.hosts.currentItem()
        selected = current.data(ROLE) if current else None
        self.hosts.clear()
        for host in self.session.app.profiles.list():
            item = QListWidgetItem(host["name"], self.hosts)
            item.setToolTip(f'{host["user"]}@{host["host"]}:{host["port"]}')
            item.setData(ROLE, host["id"])
            if selected == host["id"]:
                self.hosts.setCurrentItem(item)

    def connect_host(self, item):
        self.tabs.currentWidget().right.set_endpoint(item.data(ROLE))

    def show_host_dialog(self, host=None):
        dialog = HostDialog(self.session, host, self)
        changed = dialog.exec()
        if changed and dialog.saved:
            pane = self.tabs.currentWidget().right
            if host:
                for side in (self.tabs.currentWidget().left, pane):
                    if side.host == host["id"]:
                        side.reload()
            else:
                pane.set_endpoint(dialog.saved["id"])
        dialog.deleteLater()

    @Slot()
    def add_host(self):
        self.show_host_dialog()

    @Slot()
    def edit_host(self):
        item = self.hosts.currentItem()
        if item:
            self.show_host_dialog(self.session.app.profiles.get(item.data(ROLE)))

    @Slot()
    def remove_host(self):
        item = self.hosts.currentItem()
        if item and QMessageBox.question(self, "移除连接", f"移除「{item.text()}」？") == QMessageBox.StandardButton.Yes:
            try:
                self.session.app.profiles.remove(item.data(ROLE))
            except OSError as error:
                self.statusBar().showMessage(str(error), 6000)
            else:
                self.session.changed.emit()

    @Slot()
    def refresh_jobs(self):
        snapshot = self.session.app.jobs.list()
        if snapshot == self.last_jobs:
            return
        previous = {job["id"]: job["state"] for job in (self.last_jobs or [])}
        tab = self.tabs.currentWidget()
        if self.last_jobs is not None and any(
            job["state"] == "done" and job["workspace"] == tab.id and previous.get(job["id"]) != "done"
            for job in snapshot
        ):
            for pane in (tab.left, tab.right):
                if pane.path and not pane.address.hasFocus():
                    pane.load(pane.path)
        self.last_jobs = snapshot
        selected = {item.data(0, ROLE) for item in self.jobs.selectedItems()}
        scroll = self.jobs.verticalScrollBar().value()
        self.jobs.clear()
        for job in snapshot:
            state = PHASES.get(job["phase"], job["phase"])
            if job["bytes"]:
                state += f' · {size_text(job["bytes"])}'
            if job["error"]:
                state += " · " + job["error"]
            row = QTreeWidgetItem(self.jobs, [
                job["file"], f'{job["source"]} → {job["destination"]}', job["protocol"], state,
            ])
            row.setData(0, ROLE, job["id"])
            row.setToolTip(3, state)
            row.setSelected(job["id"] in selected)
        self.jobs.verticalScrollBar().setValue(scroll)

    @Slot()
    def cancel(self):
        for item in self.jobs.selectedItems():
            try:
                self.session.app.jobs.cancel(item.data(0, ROLE))
            except KeyError:
                continue
        self.refresh_jobs()

    def closeEvent(self, event):
        if len(self.session.windows) == 1 and any(job["state"] in ACTIVE for job in self.session.app.jobs.list()):
            response = QMessageBox.question(self, "退出 xSync", "仍有传输任务在运行。取消任务并退出？")
            if response != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.session.windows.discard(self)
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="xSync Qt desktop application")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args()
    qt = QApplication([])
    qt.setApplicationName("xSync")
    qt.setStyleSheet(STYLE)
    try:
        session = Session(args.data_dir)
    except (OSError, ValueError) as error:
        QMessageBox.critical(None, "无法启动 xSync", str(error))
        return 1
    session.new_window()
    try:
        return qt.exec()
    finally:
        session.pool.clear()
        session.app.jobs.close()
        session.pool.waitForDone()


if __name__ == "__main__":
    raise SystemExit(main())

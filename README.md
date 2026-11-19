# xSync

xSync 是使用 Qt 的轻量文件传输工具。两个面板分别选择本机或已保存的 SSH 主机。每个标签页可以独立浏览，多个窗口共享连接列表和传输队列。

## Windows 便携版

解压 `xSync-Windows-x64.zip`，双击解压目录中的 `xSync.exe`。保留同目录下的 `app`、`packages` 和 `runtime`。压缩包已包含 Python 和 Qt，双击即可打开桌面窗口。

- **拖入文件**：将资源管理器里的文件或文件夹拖到 xSync 面板。放在文件夹那一行会传到该文件夹，放在空白处会传到当前目录。xSync 左右面板和同一会话的窗口也可以互拖。
- **复制粘贴**：在资源管理器按 `Ctrl+C`，然后点 xSync 的目标文件列表并按 `Ctrl+V`。也可在 xSync 里选中文件，按 `Ctrl+C` 后粘贴到另一面板。xSync 本机面板复制的文件也可以粘贴到资源管理器。
- 拖拽和粘贴默认按自动协议添加任务，保留已有同名文件。要手动指定协议、覆盖或启用远端直传，可以点击两栏之间的箭头。
- 点击任一面板的 **☆ 收藏** 保存目录。本机、每个 SSH 连接和每个 WSL 发行版各自保存收藏。文件夹的右键菜单也可以直接收藏。
- 点击 **＋ 新标签** 或按 `Ctrl+T` 开新工作区，标签可以拖动排序和双击改名。右键文件列表可复制、粘贴、新建文件夹、重命名和永久删除。
- 传输队列可用右键取消、重试或移除已结束的记录。单文件 SFTP、rsync 和 SCP 下载会显示可获取的进度、速率和剩余时间。SFTP 文件夹传输按已传字节显示进度，无需事先遍历整棵目录。OpenSSH SCP 上传在完成时更新字节数。

### WSL

在 Windows 启用 WSL 并安装发行版后，从面板的端点下拉框选择 **WSL · 发行版名称**。可以输入 `/home` 这样的 Linux 路径，也可以使用 `\\wsl.localhost\发行版\` 或 `\\wsl$\发行版\` 路径。文件可在 WSL、本机和远端之间复制。新装发行版后可点击 **刷新 WSL**。Windows 账户可访问的发行版文件由 WSL 共享提供，使用该发行版默认用户的权限。

## 连接与传输

1. 点击 **添加连接**，输入主机名、端口和 SSH 用户名。使用 SSH agent、默认 SSH 密钥或选择本机上的私钥文件。
2. 点击 **获取主机指纹**，与管理员提供的 SHA-256 指纹核对后保存。遇到主机换钥时，重新核对并更新连接。
3. 选择两个端点并打开目录。传输按钮也接受绝对路径，可用于 SFTP 浏览不可用的服务器。

远端浏览复用 SSH/SFTP 连接，短暂缓存最近访问的目录。点击 **刷新** 可以立即读取服务器上的最新结果。SFTP 下载采用有界的并发预读，在高延迟链路上减少逐块往返。

| 协议 | 使用条件与行为 |
| --- | --- |
| rsync | 自动模式优先使用 rsync。两台远端之间默认经本机临时目录中转，结束后清理。Windows 上将兼容版本的 rsync 加入 `PATH`，或通过 `XSYNC_RSYNC` 指定程序路径。Cygwin 路径为默认格式，MSYS2 版本可设置 `XSYNC_RSYNC_STYLE=msys`。 |
| 直连 rsync | 两个面板都是远端时，可勾选 **尝试远端直传 rsync**。源主机需要能用自己的密钥登录目标。两台远端具备 rsync 时，这条路径也可在本机缺少 rsync 的情况下运行。 |
| SFTP | rsync 不可用时尝试 SFTP，便携版自带支持。两端都支持 SFTP 时，数据按块经本机中转。远端目录浏览使用 SFTP。 |
| SCP | 需要 Windows OpenSSH 客户端。自动模式可降级到传统 SCP，也支持一端 SCP、另一端 SFTP。SCP 路径仅支持单个常规文件。 |

远端 rsync 和 SCP 路径需要 POSIX 兼容 shell。传输开始后的错误会显示在队列，目录传输取消后已传完的文件会保留。连接配置保存在当前用户的应用配置目录。队列和远端文件剪贴板保留在当前 xSync 会话。

## 从源码运行和构建

Windows 使用 Python 3.12 或 3.13。在 PowerShell 安装依赖并启动：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-qt.txt
.\.venv\Scripts\python.exe -m xsync.desktop
```

以后也可以双击 `start-xsync.cmd`。

Linux 开发和测试：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-qt.txt
.venv/bin/python -m xsync.desktop
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

真实 SSH 集成测试使用临时 OpenSSH 服务，需要 `sshd`、`ssh-keygen` 和 `rsync`。可用 `--data-dir` 指定独立的配置目录。原先的本机浏览器界面可通过 `python -m xsync` 启动。

在 Linux 交叉构建 Windows x64 便携包：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ziglang==0.15.2
.venv/bin/python scripts/build_windows.py
```

输出位于 `dist/xSync-Windows-x64.zip`，同目录生成 SHA-256 校验文件。

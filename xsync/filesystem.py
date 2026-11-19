import ntpath
import os
import posixpath
import shutil
import stat
from contextlib import nullcontext

from .connections import endpoint_path, sftp_client
from .transfers import Filesystem, verify_name


WSL_PREFIX = "wsl:"


def wsl_distros():
    if os.name != "nt":
        return []
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss") as key:
            names = []
            for index in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, index)) as item:
                        name, _ = winreg.QueryValueEx(item, "DistributionName")
                    wsl_root(WSL_PREFIX + name)
                except (OSError, ValueError):
                    continue
                names.append(name)
    except OSError:
        return []
    return sorted(set(names), key=str.casefold)


def is_wsl(endpoint):
    return bool(endpoint and endpoint.startswith(WSL_PREFIX))


def remote_endpoint(endpoint):
    return endpoint if endpoint and not is_wsl(endpoint) else None


def wsl_root(endpoint):
    distro = endpoint.removeprefix(WSL_PREFIX)
    if not distro or distro in (".", "..") or any(char in distro for char in '\\/:\0'):
        raise ValueError("WSL 发行版名称无效。")
    return "\\\\wsl.localhost\\" + distro + "\\"


def wsl_path(endpoint, value=""):
    root = wsl_root(endpoint)
    if value.startswith("/") and not value.startswith("//"):
        value = root + value.lstrip("/").replace("/", "\\")
    if value.lower().startswith("\\\\wsl$\\"):
        root = root.replace("wsl.localhost", "wsl$", 1)
    path = ntpath.normpath(value or root)
    if ntpath.normcase(ntpath.splitdrive(path)[0]) != ntpath.normcase(ntpath.splitdrive(root)[0]):
        raise ValueError("路径必须位于所选 WSL 发行版中。")
    if not ntpath.isabs(path):
        raise ValueError("请输入 WSL 发行版内的绝对路径。")
    return path


def remove_remote(sftp, path):
    if not stat.S_ISDIR(sftp.lstat(path).st_mode):
        sftp.remove(path)
        return
    # Directory results must be consumed before issuing other requests on this channel.
    names = Filesystem(sftp).names(path, lambda: None)
    for name in names:
        verify_name(name, windows=False)
        remove_remote(sftp, posixpath.join(path, name))
    sftp.rmdir(path)


def operate(host, operation, paths, name=""):
    if not paths or operation not in {"rename", "mkdir", "delete"}:
        raise ValueError("请选择文件或文件夹。")
    with (sftp_client(host) if host else nullcontext()) as sftp:
        fs = Filesystem(sftp)
        for value in paths:
            path = endpoint_path(value, bool(host))
            if operation == "mkdir":
                fs.mkdir(fs.paths.join(path, verify_name(name, windows=not host and os.name == "nt")))
                return
            parent, old_name = fs.paths.split(path)
            if not old_name:
                raise ValueError("无法修改根目录。")
            if operation == "rename":
                name = verify_name(name, windows=not host and os.name == "nt")
                if old_name == name:
                    return
                target = fs.paths.join(parent, name)
                case_only = os.name == "nt" and not sftp and old_name.casefold() == name.casefold()
                if fs.exists(target) and not (case_only and os.path.samefile(path, target)):
                    raise FileExistsError(f"目标已存在：{target}")
                if sftp:
                    sftp.rename(path, target)
                else:
                    os.rename(path, target)
                return
            if sftp:
                remove_remote(sftp, path)
            elif stat.S_ISDIR(fs.info(path).st_mode):
                shutil.rmtree(path)
            else:
                fs.remove(path)

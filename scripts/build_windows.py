"""Cross-build a portable Windows x64 distribution using official CPython and Qt binaries."""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.17"
PYTHON_PACKAGE = f"python.{PYTHON_VERSION}.nupkg"
PYTHON_URL = f"https://api.nuget.org/v3-flatcontainer/python/{PYTHON_VERSION}/{PYTHON_PACKAGE}"
QT_VERSION = "6.8.3"
LICENSE_URL = f"https://raw.githubusercontent.com/qt/qtbase/v{QT_VERSION}/LICENSES/"
CHECKSUMS = {
    PYTHON_PACKAGE: "dce409744b088f94c6e37ba41f8100c384c27e029e843237364c602d30d475e5",
    "GPL-3.0-only.txt": "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    "LGPL-3.0-only.txt": "da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768",
}
QT_MODULES = {"QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll"}
QT_PLUGINS = {"plugins/platforms/qwindows.dll", "plugins/platforms/qoffscreen.dll",
              "plugins/styles/qmodernwindowsstyle.dll"}


def fetch(cache, name, url):
    download = cache / name
    if not download.is_file():
        temporary = download.with_suffix(".download")
        with urllib.request.urlopen(url, timeout=45) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out)
        temporary.replace(download)
    with download.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    if digest != CHECKSUMS[name]:
        raise RuntimeError(f"Checksum verification failed: {download}")
    return download


def unpack(archive, directory, select):
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            member = PurePosixPath(entry.filename)
            target = select(member)
            if entry.is_dir() or target is None:
                continue
            if target.is_absolute() or any(part == ".." or "\\" in part or ":" in part for part in target.parts):
                raise ValueError(f"Unsafe archive member: {target}")
            output = directory.joinpath(*target.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, output.open("wb") as out:
                shutil.copyfileobj(source, out)


def python_files(member):
    if member.parts[0] != "tools" or len(member.parts) < 2:
        return None
    excluded = {"ensurepip", "idlelib", "site-packages", "test", "tkinter", "turtledemo"}
    if member.parts[1] not in {"Lib", "DLLs"}:
        if len(member.parts) > 2 or member.suffix not in {".dll", ".exe", ".txt"}:
            return None
    elif member.parts[1] == "Lib" and len(member.parts) > 2 and member.parts[2] in excluded:
        return None
    return member.relative_to("tools")


def wheel_files(member):
    if member.parts[0] != "PySide6":
        return member
    relative = member.relative_to("PySide6")
    name = relative.as_posix()
    if name in QT_MODULES or name in QT_PLUGINS:
        return member
    if len(relative.parts) == 1 and (name in {"__init__.py", "_config.py", "_git_pyside_version.py",
                                            "pyside6.abi3.dll", "concrt140.dll"} or
                                    name.startswith(("msvcp140", "vcruntime140")) and name.endswith(".dll")):
        return member
    if relative.parts and relative.parts[0] in {"support", "scripts"} and relative.suffix == ".py":
        return member
    return None


def build(cache, stage, output):
    runtime = stage / "runtime"
    unpack(fetch(cache, PYTHON_PACKAGE, PYTHON_URL), runtime, python_files)
    (runtime / "python313._pth").write_text("Lib\nDLLs\n.\n../app\n../packages\n", encoding="ascii")
    requirements = (ROOT / "requirements-qt.txt").read_bytes() + (ROOT / "requirements.txt").read_bytes()
    wheels = cache / ("wheels-" + hashlib.sha256(requirements).hexdigest()[:12])
    wheels.mkdir(exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "pip", "download", "--dest", str(wheels), "--only-binary=:all:",
        "--platform", "win_amd64", "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
        "-r", str(ROOT / "requirements-qt.txt"),
    ], check=True)
    for wheel in sorted(wheels.glob("*.whl")):
        unpack(wheel, stage / "packages", wheel_files)
    app = stage / "app"
    (app / "xsync").mkdir(parents=True)
    for source in (ROOT / "xsync").glob("*.py"):
        shutil.copy2(source, app / "xsync")
    shutil.copy2(ROOT / "scripts" / "windows_entry.py", app / "run.py")
    shutil.copy2(ROOT / "README.md", stage)
    licenses = stage / "licenses"
    licenses.mkdir()
    for name in ("GPL-3.0-only.txt", "LGPL-3.0-only.txt"):
        shutil.copy2(fetch(cache, name, LICENSE_URL + name), licenses)
    (licenses / "THIRD-PARTY.txt").write_text(
        f"CPython {PYTHON_VERSION}: runtime/LICENSE.txt; source https://github.com/python/cpython/tree/v{PYTHON_VERSION}\n"
        f"Qt {QT_VERSION}: LGPL-3.0; source https://github.com/qt/qtbase/tree/v{QT_VERSION}\n"
        f"PySide6 and Shiboken {QT_VERSION}: LGPL-3.0; source "
        f"https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v{QT_VERSION}\n"
        "Qt is loaded dynamically from the packages directory. You may replace those libraries.\n"
        "Other Python dependency notices are in their packages/*.dist-info directories.\n",
        encoding="utf-8",
    )
    environment = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(cache / "zig-cache"),
                       ZIG_LOCAL_CACHE_DIR=str(cache / "zig-local"))
    subprocess.run([
        sys.executable, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", "-O2",
        "-municode", "-Wl,--subsystem,windows", str(ROOT / "scripts" / "windows_launcher.c"),
        "-o", str(stage / "xSync.exe"),
    ], env=environment, check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="xsync-", suffix=".zip", dir=output.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, Path("xSync") / path.relative_to(stage))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    with output.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"{output} ({output.stat().st_size / 1024 ** 2:.1f} MiB)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".windows-build")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "xSync-Windows-x64.zip")
    args = parser.parse_args()
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xsync-windows-", dir=cache) as directory:
        build(cache, Path(directory), args.output.resolve())


if __name__ == "__main__":
    main()

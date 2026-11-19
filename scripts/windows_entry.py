import ctypes
import os
import sys
from pathlib import Path


if __name__ == "__main__":
    qt = Path(__file__).resolve().parent.parent / "packages" / "PySide6"
    os.environ["QT_PLUGIN_PATH"] = str(qt / "plugins")
    try:
        with os.add_dll_directory(str(qt)):
            from xsync.desktop import main

            raise SystemExit(main())
    except Exception as error:
        if sys.stderr is not None:
            raise
        ctypes.windll.user32.MessageBoxW(None, str(error), "xSync", 0x10)
        raise SystemExit(1)

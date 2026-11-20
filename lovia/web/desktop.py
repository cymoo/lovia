"""Show a workspace file in this machine's file manager, or open it in an editor.

Both act on the machine lovia web runs on, so they make sense only when the
browser is on it too — the API mounts them on an unauthenticated (loopback)
server alone (:attr:`RouterDeps.desktop`).

Neither ever *runs* the file. Revealing selects it in a window (``open -R``,
``explorer /select``), never opens it — a ``Foo.app`` is a folder, and opening
it would launch it. Editing hands the path to an editor: the app the system
opens this file type with, when that app is a known editor, else the plain-text
editor. The type's own app may not be one — ``.ts`` is a video to macOS, a
``.py`` runs under Python Launcher or ``py.exe``, a ``.sh`` in a terminal.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path, PureWindowsPath
from typing import Literal

Reveal = Literal["finder", "explorer", "folder"]

# macOS bundle ids. A trailing "." is a prefix (every JetBrains IDE).
_MAC_EDITORS = (
    "com.microsoft.VSCode",
    "com.microsoft.VSCodeInsiders",
    "com.vscodium",
    "com.todesktop.230313mzl4w4u92",  # Cursor
    "com.exafunction.windsurf",
    "dev.zed.Zed",
    "dev.zed.Zed-Preview",
    "com.jetbrains.",
    "com.google.android.studio",
    "com.sublimetext.",
    "com.panic.Nova",
    "com.barebones.bbedit",
    "com.macromates.TextMate",
    "com.coteditor.CotEditor",
    "abnerworks.Typora",
    "com.apple.dt.Xcode",
    "com.apple.TextEdit",
)

# Windows executable names, lowercased.
_WIN_EDITORS = frozenset(
    {
        "code.exe",
        "code - insiders.exe",
        "vscodium.exe",
        "cursor.exe",
        "windsurf.exe",
        "zed.exe",
        "sublime_text.exe",
        "notepad++.exe",
        "notepad.exe",
        "devenv.exe",
        "studio64.exe",
        *(
            f"{ide}64.exe"
            for ide in (
                "idea",
                "pycharm",
                "webstorm",
                "goland",
                "clion",
                "rider",
                "phpstorm",
                "rubymine",
                "datagrip",
                "rustrover",
            )
        ),
    }
)


class LaunchError(Exception):
    """The system tool didn't take the request (it exited non-zero)."""


def reveal_kind() -> Reveal:
    """What revealing does here, for the menu to say."""
    if sys.platform == "darwin":
        return "finder"
    if sys.platform == "win32":
        return "explorer"
    return "folder"


def can_edit() -> bool:
    """Whether :func:`edit` is offered on this platform (Linux: not yet)."""
    return sys.platform in ("darwin", "win32")


def reveal(path: Path) -> None:
    """Select ``path`` in a file manager window (Linux: open its folder)."""
    if sys.platform == "darwin":
        _launch(["open", "-R", str(path)])
    elif sys.platform == "win32":
        # explorer parses its own command line: a comma ends /select's
        # argument unless the path is quoted (and a path never holds `"`).
        _launch_detached(f'explorer /select,"{path}"')
    else:
        _launch(["xdg-open", str(path if path.is_dir() else path.parent)])


def edit(path: Path) -> None:
    """Open the file ``path`` in an editor (see the module docstring)."""
    if sys.platform == "darwin":
        app = _mac_app_for(path)
        _launch(["open", "-a", app, str(path)] if app else ["open", "-t", str(path)])
    elif sys.platform == "win32":
        _launch_detached([_win_editor_for(path), str(path)])
    else:
        raise LaunchError("opening in an editor is not supported on this platform")


def _is_mac_editor(bundle_id: str) -> bool:
    return any(
        bundle_id.startswith(e) if e.endswith(".") else bundle_id == e
        for e in _MAC_EDITORS
    )


def _mac_app_for(path: Path) -> str | None:
    """The app macOS opens ``path`` with, when it is a known editor."""
    found = _mac_default_app(path)
    if found is None:
        return None
    app, bundle_id = found
    return app if _is_mac_editor(bundle_id) else None


def _win_editor_for(path: Path) -> str:
    """The exe Windows opens ``path``'s type with, when it is a known editor;
    else the one it opens ``.txt`` with; else Notepad."""
    exe = _win_assoc(path.suffix) if path.suffix else None
    if exe and PureWindowsPath(exe).name.lower() in _WIN_EDITORS:
        return exe
    # Whatever the user reads text in — that is what .txt is for.
    return _win_assoc(".txt") or "notepad.exe"


def _launch(argv: list[str]) -> None:
    """Run a launcher that hands off and exits (``open``, ``xdg-open``);
    a non-zero exit means it refused. One still running after a few seconds
    (an ``xdg-open`` that waits on its app) is left to finish on its own."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.communicate, daemon=True).start()
        return
    if proc.returncode:
        raise LaunchError(err.decode(errors="replace").strip() or argv[0])


def _launch_detached(cmd: str | list[str]) -> None:
    """Start a Windows program that outlives this server; its exit code says
    nothing (explorer's is 1 on success)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def _mac_default_app(path: Path) -> tuple[str, str] | None:
    """``(app path, bundle id)`` of the app LaunchServices opens ``path`` with."""
    import ctypes
    import plistlib

    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    ls = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_bool,
    ]
    cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
    cf.CFURLGetFileSystemRepresentation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_char_p,
        ctypes.c_long,
    ]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ls.LSCopyDefaultApplicationURLForURL.restype = ctypes.c_void_p
    ls.LSCopyDefaultApplicationURLForURL.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]

    raw = bytes(path)
    url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), path.is_dir())
    if not url:
        return None
    try:
        app_url = ls.LSCopyDefaultApplicationURLForURL(url, 0xFFFFFFFF, None)
    finally:
        cf.CFRelease(url)
    if not app_url:
        return None
    try:
        buf = ctypes.create_string_buffer(4096)
        if not cf.CFURLGetFileSystemRepresentation(app_url, True, buf, len(buf)):
            return None
    finally:
        cf.CFRelease(app_url)
    app = buf.value.decode()
    try:
        with open(Path(app, "Contents", "Info.plist"), "rb") as fh:
            bundle_id = plistlib.load(fh).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException):
        return None
    return (app, bundle_id) if isinstance(bundle_id, str) else None


def _win_assoc(ext: str) -> str | None:
    """The executable Windows opens ``ext`` files with (None if not an exe —
    a store app, say — or none: the "How do you want to open this?" picker)."""
    import ctypes
    from ctypes import wintypes

    assoc = ctypes.windll.shlwapi.AssocQueryStringW  # type: ignore[attr-defined]
    assoc.restype = ctypes.c_long  # HRESULT
    assoc.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    size = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    # 2 = ASSOCSTR_EXECUTABLE; flags 0; the "open" verb.
    if assoc(0, 2, ext, "open", buf, ctypes.byref(size)) != 0:
        return None
    exe = buf.value
    name = PureWindowsPath(exe).name.lower()
    if not name.endswith(".exe") or name == "openwith.exe":
        return None
    return exe if Path(exe).is_file() else None

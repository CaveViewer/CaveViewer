# -*- mode: python ; coding: utf-8 -*-

import os
import re

from PyInstaller.utils.hooks import collect_submodules


def _read_version_info():
    repo_root = os.getcwd()
    version_file = os.path.join(repo_root, "caveviewer_version.py")
    with open(version_file, "r", encoding="utf-8") as f:
        content = f.read()

    app_name_match = re.search(r'^APP_NAME = "([^"]+)"$', content, re.MULTILINE)
    app_version_match = re.search(r'^APP_VERSION = "([^"]+)"$', content, re.MULTILINE)

    app_name = app_name_match.group(1) if app_name_match else "CaveViewer"
    app_version = app_version_match.group(1) if app_version_match else "0.0.0"
    return app_name, app_version


APP_NAME, APP_VERSION = _read_version_info()

hiddenimports = []
hiddenimports += collect_submodules("moderngl")
hiddenimports += collect_submodules("moderngl_window")

# Bundle the compiled C draw-loop extension if it was built before packaging.
# The import in viewer_window.py is wrapped in try/except so PyInstaller's
# analysis phase won't detect it automatically -- we include it explicitly
# here. If the .so hasn't been built, binaries stays empty and the app falls
# back to the Python draw loop without error.
import glob as _glob
_drawbatch_binaries = [
    (p, "core")
    for p in _glob.glob("core/drawbatch*.so") + _glob.glob("core/drawbatch*.pyd")
]


a = Analysis(
    ["caveviewer.py"],
    pathex=[],
    binaries=_drawbatch_binaries,
    datas=[
        ("shaders", "shaders"),
        ("gui/assets", "gui/assets"),
        ("gui/updater.py", "gui"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CaveViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
app = BUNDLE(
    exe,
    name="CaveViewer.app",
    icon=os.environ.get("CAVEVIEWER_APP_ICON") or None,
    bundle_identifier=None,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "CFBundleGetInfoString": f"{APP_NAME} {APP_VERSION}",
    },
)

# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os


project_root = Path(globals().get('SPECPATH', os.getcwd())).resolve()

a = Analysis(
    [str(project_root / 'caveviewer.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / 'shaders'), 'shaders'),
        (str(project_root / 'gui' / 'assets'), 'gui/assets'),
    ],
    hiddenimports=['PIL._tkinter_finder', 'tkinter', 'moderngl_window.context.pyglet'],
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
    [],
    exclude_binaries=True,
    name='CaveViewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CaveViewer',
)

bundle_kwargs = {
    'name': 'CaveViewer.app',
    'bundle_identifier': None,
}
app_icon = os.environ.get('CAVEVIEWER_APP_ICON', '').strip()
if app_icon:
    bundle_kwargs['icon'] = app_icon

app = BUNDLE(
    coll,
    **bundle_kwargs,
)

# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['/workspace/caveviewer.py'],
    pathex=[],
    binaries=[],
    datas=[('/workspace/shaders', 'shaders'), ('/workspace/gui/assets', 'gui/assets')],
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

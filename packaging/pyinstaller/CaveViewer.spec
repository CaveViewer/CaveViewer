# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_all


spec_dir = Path(globals().get('SPECPATH', os.getcwd())).resolve()
project_root = spec_dir
while project_root.parent != project_root and not (project_root / 'pyproject.toml').is_file():
    project_root = project_root.parent
if not (project_root / 'pyproject.toml').is_file():
    raise RuntimeError(f'Could not locate CaveViewer project root from {spec_dir}')

source_root = project_root / 'src'
package_root = source_root / 'caveviewer'
resources_root = package_root / 'resources'
version_ns = {}
exec((package_root / 'version.py').read_text(encoding='utf-8'), version_ns)
app_name = version_ns.get('APP_NAME', 'CaveViewer')
app_version = version_ns.get('APP_VERSION', '0.0.0')

hidden_imports = ['PIL._tkinter_finder', 'tkinter']
extra_binaries = []
extra_datas = []
if sys.platform.startswith('linux'):
    glfw_datas, glfw_binaries, glfw_hidden_imports = collect_all('glfw')
    extra_datas.extend(glfw_datas)
    extra_binaries.extend(glfw_binaries)
    hidden_imports.extend(glfw_hidden_imports)
    hidden_imports.extend([
        'dbus_fast.aio',
        'moderngl_window.context.glfw',
    ])
else:
    hidden_imports.append('moderngl_window.context.pyglet')

a = Analysis(
    [str(package_root / '__main__.py')],
    pathex=[str(source_root)],
    binaries=extra_binaries,
    datas=[
        (str(resources_root / 'shaders'), 'caveviewer/resources/shaders'),
        (str(resources_root / 'images'), 'caveviewer/resources/images'),
        (
            str(resources_root / 'release_signing_public_key.pem'),
            'caveviewer/resources',
        ),
        (
            str(resources_root / 'map_library_catalog.v1.json'),
            'caveviewer/resources',
        ),
        (
            str(resources_root / 'cave_metadata_catalog.v1.json'),
            'caveviewer/resources',
        ),
        (str(project_root / 'LICENSE'), '.'),
        (str(project_root / 'THIRD_PARTY_NOTICES.md'), '.'),
    ] + extra_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
app_icon = os.environ.get('CAVEVIEWER_APP_ICON', '').strip()

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
    console=not sys.platform.startswith('win'),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon or None,
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
    'bundle_identifier': 'com.caveviewer.CaveViewer',
    'info_plist': {
        'CFBundleName': app_name,
        'CFBundleDisplayName': app_name,
        'CFBundleShortVersionString': app_version,
        'CFBundleVersion': app_version,
    },
}
if app_icon:
    bundle_kwargs['icon'] = app_icon

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        **bundle_kwargs,
    )
else:
    # Windows and Linux packaging consume a normal one-folder payload. A macOS
    # BUNDLE here would hide that payload inside an .app wrapper on Windows.
    app = coll

# Third-Party Notices

CaveViewer is licensed under the GNU General Public License version 3.0. See
`LICENSE` for the full license text.

This project also uses third-party Python packages. The runtime dependency list
is maintained in `requirements.txt`; exact dependency versions and licenses are
resolved from package metadata when the environment is installed.

## Python Runtime Dependencies

| Package | Requirement |
|---|---|
| cryptography | `cryptography>=43.0.0` |
| dbus-fast (Linux) | `dbus-fast==5.0.22` |
| freetype-py | `freetype-py==2.5.1` |
| glfw / bundled GLFW libraries (Linux) | `glfw==2.10.0` |
| imageio-ffmpeg | `imageio-ffmpeg>=0.5.1` |
| moderngl | `moderngl==5.12.0` |
| moderngl-window | `moderngl-window==3.1.1` |
| numpy | `numpy==2.5.0` |
| Pillow | `Pillow==12.3.0` |
| pygltflib | `pygltflib==1.16.5` |
| pyglm | `pyglm>=2.7.1` |
| truststore | `truststore>=0.10.0` |

`dbus-fast` is distributed under the MIT License. The `glfw` Python binding is
distributed under the MIT License and its bundled native GLFW libraries use
the zlib/libpng license. Release notices must retain the licenses shipped by
those packages.

## Bundled Linux AppImage Font

Linux AppImage packages may bundle Noto Sans Regular from the system
`fonts-noto-core` package so CaveViewer's FreeType-rendered UI text is
consistent across GitHub-hosted release builds and user systems. Noto fonts are
licensed under the SIL Open Font License 1.1.

Before publishing a bundled binary, regenerate dependency notices from the
bundled environment and include the exact package versions and license metadata
used for that release.

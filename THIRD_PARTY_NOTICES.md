# Third-Party Notices

CaveViewer is licensed under the GNU General Public License version 3.0. See
`LICENSE` for the full license text.

This project also uses third-party Python packages. The runtime dependency list
is maintained in `requirements.txt`; exact dependency versions and licenses are
resolved from package metadata when the environment is installed.

## Python Runtime Dependencies

| Package | Requirement |
|---|---|
| freetype-py | `freetype-py==2.5.1` |
| moderngl | `moderngl==5.12.0` |
| moderngl-window | `moderngl-window==3.1.1` |
| numpy | `numpy==2.5.0` |
| Pillow | `Pillow==12.2.0` |
| pyglm | `pyglm>=2.7.1` |
| truststore | `truststore>=0.10.0` |

Before publishing a bundled binary, regenerate dependency notices from the
bundled environment and include the exact package versions and license metadata
used for that release.

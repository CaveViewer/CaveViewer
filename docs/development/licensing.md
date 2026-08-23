# Licensing

CaveViewer is licensed under the GNU Affero General Public License version 3.0
only, identified by the SPDX expression `AGPL-3.0-only`. The unmodified license
text in the repository root `LICENSE` file is authoritative.

The initial repository commit included the AGPLv3 license text, while several
metadata and user-interface declarations incorrectly described the project as
ordinary GPLv3. Those declarations were corrected to `AGPL-3.0-only`; the
license text itself was not replaced. This correction does not alter or revoke
rights already received with an earlier copy.

AGPLv3 applies to distribution of covered software and includes obligations for
modified covered versions offered for users to interact with over a network.
Consult the license text and qualified legal counsel for compliance decisions;
this development document is not a substitute for either.

## Releases and corresponding source

Each GitHub Release is created from an immutable `v<version>` tag. The source
corresponding to a CaveViewer release is available from that tag through the
repository and GitHub's generated source archives. Release verification must
confirm the tag resolves to the published workflow's immutable source revision.

Windows, Linux, and macOS packages include `LICENSE` and
`THIRD_PARTY_NOTICES.md`. Do not remove those files from frozen payloads,
installers, AppImages, or disk images.

## Third-party works

Dependencies and bundled assets retain their own licenses. Their notices are
recorded in `THIRD_PARTY_NOTICES.md` and in license metadata shipped by their
distributors. Describing CaveViewer as AGPL-3.0-only does not replace or
relicense those works. Before publishing a binary, preserve the exact notices
for the dependency versions and assets included in that build.

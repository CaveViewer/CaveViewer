# Intel update manifests

The macOS Intel (`x86_64`) release workflow writes signed `stable.json` or
`preview.json` manifests here when a corresponding Intel DMG is published.

Do not copy ARM64 manifests into this directory. Until the first Intel release
is published, a 404 for this architecture is safer than offering an
incompatible Apple Silicon package.

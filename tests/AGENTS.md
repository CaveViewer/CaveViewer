# Test instructions

Applies to: `tests/`
Inherits: `/AGENTS.md`
Overrides: none
Validation:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q
```

These rules supplement the repository-level `AGENTS.md` for files under
`tests/`. The canonical testing policy, commands, markers, and coverage floors
live in `/docs/development/testing.md`.

- Tests must be deterministic, isolated, and offline by default. Mock HTTP at
  the transport boundary; never depend on a live service or release asset.
- Use `tmp_path` and the isolated CaveViewer home fixtures for filesystem
  behavior. Do not write caches, preferences, or coverage data into the source
  tree.
- Place narrow behavior tests under `tests/unit/<area>/` and cross-component or
  real-filesystem workflows under `tests/integration/`.
- Apply the markers declared in `pyproject.toml` when a test genuinely requires
  GUI, OpenGL, network, slow, or integration behavior.
- Exercise failure cleanup, not only the raised exception. Assert that partial
  files, pending state, callbacks, threads, and resources are left consistent.
- Bound waits with events/timeouts and always stop worker threads created by a
  test.
- Prefer regression tests that fail for the reported bug before broad coverage
  tests. Do not weaken an assertion merely to accommodate nondeterminism.

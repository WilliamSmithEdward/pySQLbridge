# Notes for agents

<!-- repo-standards:begin. Copied from WilliamSmithEdward/repo-standards, templates/agents/AGENTS-block.md. Change it there; the weekly rescan fails a copy that differs. -->
## Releases, CI and security

These rules are the same in every WilliamSmithEdward repository.

- **How a release happens here:** pushing a `vX.Y.Z` tag runs Publish, which uploads to PyPI and creates the GitHub release with its security and malware reports.
- **Starting a workflow by hand never releases anything.** Publish and every
  release report are dry runs when started with `gh workflow run` or the Run
  workflow button. They build, scan and assemble the release files exactly
  as a release would, and upload them as the `release-preview` artifact
  instead. Run one after changing anything on the release path:
  `gh workflow run <file> --ref main`, then
  `gh run download <run-id> -n release-preview`.
- **Do not create, publish, edit or delete a release or a `v*` tag** unless
  the owner asks for it. A `v*` tag cannot be moved or deleted once pushed.
- **Every change to `main` goes through a pull request** that passes CI
  passed, Security passed and Malware scan passed. No one can push to `main`
  directly or skip the checks, admins included. Push a branch, open a pull
  request, and let it merge itself: `gh pr merge --auto --squash <number>`.
- **Pins.** Actions by full commit SHA with the version as a comment. Images
  by digest, in `.github/security/<tool>/Dockerfile`. Python tools from the
  hash-locked `.github/requirements/<purpose>.txt`, compiled from the `.in`
  beside it with
  `uv pip compile <purpose>.in --universal --generate-hashes --python-version 3.12 -o <purpose>.txt`.
  Runners are named releases, never `-latest`.
- **Updates merge themselves.** Dependabot and the Update YARA rules workflow
  open pull requests that merge once the three checks pass, except a
  third-party major version, which waits for the owner. Leave them alone
  unless asked.
- **A scanner finding is fixed or accepted with a written reason** in the
  repository's accepted list. Never silence a scanner without one.
<!-- repo-standards:end -->

## This repository

pySQLbridge is a TDS server published to PyPI as `pysqlbridge`. What an
agent working here must not break:

- **The release path.** `publish.yml` and its `pypi` environment keep their
  names: PyPI's trusted publisher is bound to both. A release's notes are
  its section of `CHANGELOG.md` (`## [X.Y.Z] - date`), written before the
  tag is pushed; without one the release says only "Release X.Y.Z".
- **The test install.** CI installs the hash-locked
  `.github/requirements/test.txt`, then the package with
  `pip install --no-deps --no-build-isolation -e .`. `test.txt` and
  `runtime.txt` serve Python 3.10 to 3.14, so they are compiled with
  `--python-version 3.10`, not 3.12. A new runtime dependency goes in
  `pyproject.toml`, `test.in`, `runtime.in` and, if the parsers import it,
  `fuzz.in`, each recompiled.
- **The sdist runs its own suite.** CI unpacks the sdist and runs the tests
  from it, and `MANIFEST.in` ships `tests`, `scripts`, `docs` and
  `examples` for that. Tests must not import from `fuzz/`, which the sdist
  does not ship; the fuzz targets live in `tests/fuzz_targets.py` for that
  reason.
- **Parsers raise their own error.** The TDS readers raise
  `TdsProtocolError`, the SQL parser `SqlError`, the source readers
  `SourceError`, and nothing else on bad input. The Fuzz workflow holds them
  to it. A fuzz finding is minimised into `tests/fuzz_corpus/<target>/` as
  `regression_<what>`, fixed, and tested beside the parser's own tests.
  Seeds are bytes: `.gitattributes` keeps their line endings.
- **The captures are real.** `tests/captured.py` holds bytes taken off the
  wire from SQL Server 2025 and real clients. Do not hand-edit them; they
  are regenerated with `scripts/capture_login.ps1`.
- **No live services.** `scripts/capture_login.ps1`, `differential.ps1` and
  the probes need SQL Server, Excel or Wireshark. Do not run them, or
  anything that authenticates against a real service; the unit suite needs
  none of it.

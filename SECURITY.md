# Security policy

## Reporting a vulnerability

Report a vulnerability privately, not in a public issue or pull request:
[open a private report](https://github.com/WilliamSmithEdward/pySQLbridge/security/advisories/new).
Only the maintainer sees it. Include the pySQLbridge version, the client and
driver that connected (Excel, Power BI, SSMS, sqlcmd, a .NET or ODBC driver),
the source type involved and the configuration that reaches it, and the
smallest file, response or steps that show it, with credentials and private
data removed.

A confirmed vulnerability is fixed in a release on PyPI, and the advisory is
published with it, crediting you unless you ask otherwise.

## Supported versions

Only the latest release on PyPI receives security fixes. Older releases are
not maintained separately; update when a fix ships.

## Scope

pySQLbridge is a server. It listens on a TCP port, 127.0.0.1:1337 unless
told otherwise, speaks SQL Server's TDS protocol to whatever connects, and
answers queries from the sources its configuration names: CSV, JSON, XML and
HTML files, Excel workbooks, Access databases, and HTTP APIs it fetches. It
reads those sources and never writes to them; temporary tables live in the
memory of one connection. It generates a self-signed certificate for the TLS
tunnel the login travels in.

Two kinds of input come from outside: the bytes a client sends, much of it
before any login is checked, and the content a configured source returns,
which for an HTTP API is whatever that server answers. These count as
vulnerabilities:

- client bytes or a query that crash or hang the server, or reach beyond
  the connection that sent them;
- a way to log in without valid credentials: Windows Authentication is
  checked by SSPI's `AcceptSecurityContext`, and a username and password
  only against the `"logins"` the configuration names, none by default;
- a query that reads anything but the configured sources, or writes to one;
- a source's content that makes the server read a local file or run
  anything;
- a password, token or API key from the configuration or a login reaching
  a log line, an error message or a client.

The server starts no process, runs no macro and evaluates no code from a
source. An Access database's saved queries are answered by pyOpenVBA's
Access SQL over an in-memory copy of the file.

### Exposing the port

Listening on 127.0.0.1 keeps the server to the machine it runs on. With
`--host 0.0.0.0` every client that can reach the port can try to log in.
The certificate is self-signed, so a client has to be told to trust it, and
then cannot tell this server from another one in the middle. Expose the
port only on a network you trust, and prefer Windows Authentication, which
sends no password, over a username and password.

### Credentials in the configuration

Write `"password_hash"` rather than `"password"` for a login, and give API
tokens as `${env:NAME}` rather than as literals, so the configuration file
can be committed without a secret in it.

## How the code is checked

Three workflows check every pull request and every push to `main`, and
their gates decide whether a change can merge: **CI passed**,
**Security passed** and **Malware scan passed**. A gate passes only when
every job before it did, and any unexpected finding fails it, whatever its
severity. Security also runs weekly, so new queries and rules reach code
that has not changed, and Malware scan runs daily, so new signatures and
rules reach files that have not changed.

- **Code:** CodeQL with GitHub's security-extended queries, for Python and
  GitHub Actions, and Semgrep with the default, Python, security-audit,
  secrets and GitHub Actions rule sets. Both scan the package, the
  workflows that build and publish it, and the scripts in
  `scripts/security` that judge the scans. A `nosemgrep` comment cannot
  hide a finding. Results go to the repository's code scanning.
- **Workflows:** zizmor audits the GitHub Actions workflows; a finding fails
  Security.
- **Dependencies:** pip-audit checks the runtime dependencies, cryptography,
  pyOpenVBA and pywin32, at the versions a fresh install gets today, pinned
  in `.github/requirements/runtime.txt` and moved daily by Dependabot. It
  runs on Windows, so pywin32, which only Windows installs, is audited too.
  Any known vulnerability fails Security.
- **Malware:** ClamAV, with signatures freshclam fetches and verifies on
  every run, and YARA-X, with the YARA Forge rules pinned to a release and
  its SHA-256, scan every file the commit holds, test fixtures and the
  fuzz corpus included, and the wheel and sdist built from it with the
  hash-locked build tools, as a release builds them. YARA-X runs YARA
  Forge's full rule set. A scan error fails the report as a match does.
- **Fuzzing:** Atheris drives ten targets in `fuzz/fuzz_parsers.py`: the
  TDS packet, PRELOGIN, LOGIN7, SQL batch and RPC readers a client reaches
  before it logs in, the SQL parser, and the JSON, XML, HTML and CSV
  readers a source's response goes through, on to the table built from it.
  Each starts from its seeds in `tests/fuzz_corpus`, and a parser must
  succeed or raise the one error `tests/fuzz_corpus/README.md` lists for it.
  The Fuzz workflow runs on every change to the package, the fuzz targets
  or the corpus, and daily. It is not a gate: a finding becomes a
  regression test with its fix, and a seed that `tests/test_fuzz_corpus.py`
  replays on every CI run.
- **OpenSSF Scorecard** rates the repository's security practices on every
  change to `main` and weekly, and the README badge shows the result.
  Its Code-Review and Contributors checks assume more than one
  maintainer, such as a second person approving every change, so a
  single-maintainer project cannot score full marks on them.

## Accepted findings

A finding is fixed, or accepted with a written reason in
[.github/security/accepted.toml](https://github.com/WilliamSmithEdward/pySQLbridge/blob/main/.github/security/accepted.toml)
for CodeQL and Semgrep, or
[.github/security/malware-accepted.toml](https://github.com/WilliamSmithEdward/pySQLbridge/blob/main/.github/security/malware-accepted.toml)
for ClamAV and YARA-X. An entry matches on the tool, the rule and the
file, and in accepted.toml also the text of the flagged line, so an edited
line needs another review, and an entry that no longer matches fails the
report. zizmor keeps its exceptions in `.github/zizmor.yml` or inline
beside the line they excuse, each with its reason.

The current entries:

- Semgrep `formatted-sql-query` and `sqlalchemy-execute-raw-query`, on the
  call in `access.py` that runs an Access database's saved query by the
  name the database itself gives it, over an in-memory copy of that file.
- Semgrep `dynamic-urllib-use-detected`, on the one `urlopen` call every
  HTTP source goes through, which refuses any scheme but http and https.
- Semgrep `use-defused-xml`, in `markup.py` and `workbook.py`, which parse
  XML with the standard library's ElementTree, which loads no external
  entity or DTD.
- YARA-X `SIGNATURE_BASE_Powershell_Case_Anomaly`, on the README, which
  names PowerShell in prose and in code-block labels.
- zizmor's `self-repository` and `superfluous-actions` rules are turned
  off in `.github/zizmor.yml`, each with its reason and when it comes back.

## Pinning and updates

Everything the workflows run is pinned: actions to full commit SHAs,
runners to named OS releases, scanner images to digests, Python tools and
the runtime dependencies CI tests against to hash-locked lock files, and
the YARA-X engine and YARA Forge rules to a release and its SHA-256.
`pyproject.toml` gives the package's own dependencies as minimum versions,
for the people who install it. ClamAV's signatures change too often to
pin, so freshclam fetches and verifies them on every run.

Dependabot proposes updates to the GitHub Actions, the Semgrep and ClamAV
images, the hash-locked files in `.github/requirements` and the floors in
`pyproject.toml` once a version is a week old, and at once for a security
advisory; a new pyOpenVBA does not wait. The Update YARA rules workflow
proposes new YARA pins in `.github/security/yara.json` each week. A minor
or patch update, and the YARA pull request, merges itself once CI,
Security and Malware scan pass; a third-party major version waits for
review.

## Releases

A pushed `v*` tag builds the sdist and wheel, checks that the tag matches
the version in `pyproject.toml`, runs the suite, and runs Security and
Malware scan on the tagged commit. Nothing is published unless all of them
pass. The distributions go to PyPI through Trusted Publishing, so no upload
token exists to leak. The GitHub release, titled with the tag, carries the
distributions, `pysqlbridge-<version>-security-report.md` and
`pysqlbridge-<version>-malware-report.md` beside the scan results they were
made from, and the provenance bundle, with the version's section of
`CHANGELOG.md` as its notes. Started by hand, the Publish workflow is
always a dry run and publishes nothing.

### Verifying a download

Every file on PyPI carries PyPI's own provenance, which names this
repository's `publish.yml` as the publisher; the file's page on PyPI shows
it. Releases after 2.1.2 also carry a GitHub build provenance attestation,
which you can check against any copy of the file, from PyPI or from the
GitHub release:

```
pip download pysqlbridge --no-deps -d check
gh attestation verify check/<file> --owner WilliamSmithEdward
```

The output names the commit and workflow run that built the file. The
signed bundle is also attached to the GitHub release as
`pysqlbridge-<version>.sigstore.json`, so the check works without asking
GitHub for it: add `--bundle pysqlbridge-<version>.sigstore.json`.

## Repository settings

<!-- repo-standards:begin security-settings. Copied from WilliamSmithEdward/repo-standards, templates/security/settings-block.md. Change it there; the weekly rescan fails a copy that differs. -->
- `main` accepts changes only through a pull request that passes
  **CI passed**, **Security passed** and **Malware scan passed**. The
  ruleset has no bypass, for the owner either, and refuses force-pushes and
  deleting the branch.
- A `v*` release tag cannot be moved or deleted once pushed, except by a
  repository admin.
- A workflow that uses an action not pinned to a full commit SHA fails to
  run. Workflow tokens are read-only unless a job is granted more for
  itself.
- Secret scanning with push protection, Dependabot alerts and security
  updates, and private vulnerability reporting are on.
<!-- repo-standards:end -->

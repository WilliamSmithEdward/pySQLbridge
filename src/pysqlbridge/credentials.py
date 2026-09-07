"""Proving who you are to an API this bridge reads from.

The other direction, a SQL client proving itself to this bridge, is auth.py.
Both are authentication and they share nothing: that one is Windows deciding
whether to admit a login, this one is a secret being put somewhere an upstream
API will look for it.

Four mechanisms cover essentially every public HTTP API worth pointing at:

    Authorization: Bearer <token>       RFC 6750, OAuth 2 and most modern APIs
    Authorization: Basic <base64>       RFC 7617
    X-API-Key: <key>                    a key in a header of the API's choosing
    ?api_key=<key>                      a key in the query string

They differ only in where the secret goes, so they are one type with a place
rather than four classes.

Secrets are read from the environment by default. A configuration file gets
committed, and a token in a committed file is a leaked token, so the config
holds the name of a variable and the value stays outside it. A literal is
still accepted, because someone testing against a local server should not have
to set an environment variable first, but nothing here ever puts a secret in a
message, a log line, or a repr: an authentication failure prints where the
credential came from, never what it was.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.parse
from dataclasses import dataclass, field

from .source import SourceError

# ${VAR} or ${env:VAR}. The env: form is noise-free about intent, the bare form
# is what people write.
_REFERENCE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")

SCHEMES = ("bearer", "basic", "header", "query")

# Header authentication defaults to sending the key as-is. A scheme word in
# front of it, "Token abc123", is set with "prefix".
DEFAULT_HEADER = "Authorization"


def resolve(value: str, *, what: str) -> str:
    """Expand ${VAR} references, or explain which variable was not set.

    Substitution rather than replacement of the whole string, so a credential
    can be a template: "Token ${GITHUB_PAT}" works, and so does a bare
    "${GITHUB_PAT}".
    """
    missing: list[str] = []

    def substitute(match: re.Match) -> str:
        name = match.group(1)
        found = os.environ.get(name)
        if found is None:
            missing.append(name)
            return ""
        return found

    expanded = _REFERENCE.sub(substitute, value)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise SourceError(
            f"{what} refers to {names}, which is not set in the environment"
        )
    return expanded


@dataclass(frozen=True)
class Credential:
    """A secret and where to put it.

    The secret is kept out of the repr on purpose. These end up in tracebacks,
    log lines and debugger views, and a token that reaches any of those has to
    be treated as disclosed.
    """

    scheme: str
    secret: str = field(repr=False)
    name: str = DEFAULT_HEADER
    prefix: str = ""
    source: str = "a literal"

    def __post_init__(self) -> None:
        if self.scheme not in SCHEMES:
            raise SourceError(
                f"'{self.scheme}' is not an authentication scheme; use one of "
                f"{', '.join(SCHEMES)}"
            )
        if not self.secret:
            raise SourceError(f"the credential from {self.source} is empty")

    def __str__(self) -> str:
        where = f" in {self.name}" if self.scheme in ("header", "query") else ""
        return f"{self.scheme} credential{where}, from {self.source}"

    def apply(self, url: str, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
        """The URL and headers to send, with the credential in place.

        Neither argument is modified. A source's headers are shared across the
        threads racing its mirrors, and a credential that rewrites them in
        place would be a data race for no benefit.
        """
        if self.scheme == "query":
            return with_parameter(url, self.name, self.secret), dict(headers)

        sent = dict(headers)
        if self.scheme == "bearer":
            sent[DEFAULT_HEADER] = f"Bearer {self.secret}"
        elif self.scheme == "basic":
            sent[DEFAULT_HEADER] = f"Basic {self.secret}"
        else:
            sent[self.name] = f"{self.prefix} {self.secret}".strip()
        return url, sent


def with_parameter(url: str, name: str, value: object) -> str:
    """Set a query parameter, keeping whatever else the URL already carried.

    Used for putting a key in the query string, and by the paging code for
    advancing an offset. It lives here rather than next to either caller
    because this module sits below both of them in the import graph.
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != name]
    query.append((name, str(value)))
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query))
    )


def credential(spec: object, *, what: str = "auth") -> Credential | None:
    """Build a credential from its configured form.

        {"bearer": "${GITHUB_TOKEN}"}
        {"basic": {"username": "someone", "password": "${API_PASSWORD}"}}
        {"header": "X-API-Key", "value": "${WEATHER_KEY}"}
        {"header": "Authorization", "prefix": "Token", "value": "${PAT}"}
        {"query": "api_key", "value": "${NASA_KEY}"}
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise SourceError(f'{what} must be an object, one of {", ".join(SCHEMES)}')

    named = [s for s in SCHEMES if s in spec]
    if len(named) != 1:
        raise SourceError(
            f"{what} must name exactly one of {', '.join(SCHEMES)}"
            + (f", not {len(named)}" if named else "")
        )
    scheme = named[0]
    origin = _origin(spec, scheme)

    if scheme == "bearer":
        return Credential("bearer", resolve(str(spec["bearer"]), what=what),
                          source=origin)

    if scheme == "basic":
        pair = spec["basic"]
        if not isinstance(pair, dict) or "password" not in pair:
            raise SourceError(f'{what}: basic needs a username and a password')
        user = resolve(str(pair.get("username", "")), what=what)
        password = resolve(str(pair["password"]), what=what)
        encoded = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        return Credential("basic", encoded, source=origin)

    if "value" not in spec:
        raise SourceError(f'{what}: {scheme} authentication needs a "value"')
    return Credential(
        scheme=scheme,
        secret=resolve(str(spec["value"]), what=what),
        name=str(spec[scheme]),
        prefix=str(spec.get("prefix", "")),
        source=origin,
    )


def _origin(spec: dict, scheme: str) -> str:
    """Where the secret came from, safe to print.

    A credential that fails is usually a variable that was not set or a value
    that was pasted wrong, and knowing which of those it is means knowing where
    it came from. The value itself never helps and cannot be shown.
    """
    literal = spec.get("value")
    if scheme == "bearer":
        literal = spec.get("bearer")
    elif scheme == "basic":
        pair = spec.get("basic")
        literal = pair.get("password") if isinstance(pair, dict) else None
    found = _REFERENCE.findall(str(literal or ""))
    return f"${{{found[0]}}}" if found else "a literal in the configuration"

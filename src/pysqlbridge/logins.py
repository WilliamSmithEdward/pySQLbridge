"""Deciding whether a username and password may connect.

This is the SQL authentication half of admitting a client. The Windows half is
auth.py, where the decision belongs to Windows; here the decision belongs to
this file, which is a much more serious thing to own, so the rules are narrow
and stated:

- Nothing is admitted unless the configuration names it. A bridge with no
  "logins" section refuses every username and password there is, rather than
  admitting anyone. Failing closed is the only safe default for a check whose
  whole job is to say no.
- A password is never written down by this process. It is not logged, not put
  in a message, not in a repr, and not in an exception. A failure says which
  login was refused and never what was offered for it.
- Every refusal is the same refusal. An unknown user and a wrong password
  return one answer, computed in the same way and at the same cost, so that
  neither the message nor the time it took says which of the two it was.
- A stored hash is preferred to a stored password. A hash can sit in a
  configuration file that gets committed; a password cannot, so a password
  belongs in the environment and the config holds only its variable's name.

What this does not do is protect the password on the wire, because it cannot:
the LOGIN7 password field is obfuscated with a published constant, which is
not encryption. What protects it is the tunnel, which covers the login packet
even on a connection that agreed to encrypt nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .credentials import resolve
from .source import SourceError

LOGIN_KEYS = frozenset({"user", "password", "password_hash"})

# The only algorithm written here. Stored hashes name their own, so a hash
# made by an older version keeps working when this changes.
ALGORITHM = "pbkdf2_sha256"

# Measured on this machine at 23 ms, which is a cost worth paying once per
# connection and not worth paying in a loop. The count travels inside each
# stored hash, so raising it later does not invalidate the hashes already
# written.
ITERATIONS = 210_000

SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = ITERATIONS,
                  salt: bytes | None = None) -> str:
    """A password as it can safely be written in a configuration file.

    The salt is per password, so two accounts that chose the same password do
    not have the same hash, and a stolen file does not say which they were.
    """
    if not password:
        raise SourceError("a password to hash cannot be empty")
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 iterations)
    return "$".join((
        ALGORITHM,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def _matches_hash(stored: str, offered: str) -> bool:
    """Whether a password produces a stored hash, in constant time."""
    unreadable = SourceError(
        f'a stored password hash is not readable; it should look like '
        f'"{ALGORITHM}$<iterations>$<salt>$<hash>"'
    )
    parts = stored.split("$")
    if len(parts) != 4:
        raise unreadable
    algorithm, written_iterations, salt, digest = parts
    # Named before anything is decoded, so a hash from some other tool is
    # told what this understands rather than that its base64 is the wrong
    # length, which is true but unhelpful.
    if algorithm != ALGORITHM:
        raise SourceError(
            f"a stored password hash names '{algorithm}', and this "
            f"understands only {ALGORITHM}"
        )
    try:
        iterations = int(written_iterations)
        salt_bytes = base64.b64decode(salt, validate=True)
        digest_bytes = base64.b64decode(digest, validate=True)
    except (ValueError, TypeError) as exc:
        raise unreadable from exc
    computed = hashlib.pbkdf2_hmac("sha256", offered.encode("utf-8"),
                                   salt_bytes, iterations)
    return hmac.compare_digest(computed, digest_bytes)


@dataclass(frozen=True)
class Login:
    """One account that may connect, and the secret that proves it.

    secret is kept out of the repr for the same reason a credential's is:
    these end up in tracebacks and debugger views, and a password that reaches
    either has to be treated as disclosed.
    """

    user: str
    secret: str = field(repr=False)
    hashed: bool
    source: str = "the configuration"

    def __str__(self) -> str:
        kept = "a hash" if self.hashed else "a password"
        return f"login {self.user!r}, {kept} from {self.source}"

    def admits(self, password: str) -> bool:
        if self.hashed:
            return _matches_hash(self.secret, password)
        return hmac.compare_digest(self.secret, password)


class LoginStore:
    """The accounts a bridge admits, and the question a connection asks it.

    Usernames are matched without regard to case, which is how this server
    matches all text and what a client connecting to a default installation
    expects. Passwords are matched exactly.
    """

    def __init__(self, logins: list[Login] | None = None) -> None:
        self._logins = {login.user.lower(): login for login in (logins or [])}
        # Something for an unknown user to be checked against, so that no
        # answer is reached faster than any other. Its password is random and
        # nothing knows it, so it can never admit anyone.
        self._decoy = hash_password(secrets.token_urlsafe(32))

    def __len__(self) -> int:
        return len(self._logins)

    @property
    def configured(self) -> bool:
        """Whether any account was named. Nothing is admitted when none was."""
        return bool(self._logins)

    @property
    def names(self) -> list[str]:
        """The account names, for a log line at startup. Never the secrets."""
        return [login.user for login in self._logins.values()]

    def admits(self, user: str, password: str) -> bool:
        """Whether this username and password may connect.

        An unknown user is checked against a decoy rather than returned on at
        once, so that the time taken does not say whether the name exists.
        """
        found = self._logins.get((user or "").lower())
        if found is None:
            _matches_hash(self._decoy, password)
            return False
        return found.admits(password)


def _login_from(entry: object, position: int, where: str) -> Login:
    if not isinstance(entry, dict):
        raise SourceError(f"{where} login {position} is not an object")
    for key in entry:
        if key not in LOGIN_KEYS:
            raise SourceError(
                f'{where} login {position}: "{key}" is not a login option; '
                f"use one of {', '.join(sorted(LOGIN_KEYS))}"
            )

    user = entry.get("user")
    if not isinstance(user, str) or not user:
        raise SourceError(f'{where} login {position} needs a "user"')

    written, hashed = entry.get("password_hash"), True
    if written is None:
        written, hashed = entry.get("password"), False
    if written is None:
        raise SourceError(
            f'{where} login {position} needs a "password_hash", or a '
            f'"password" naming an environment variable'
        )
    if not isinstance(written, str):
        raise SourceError(
            f"{where} login {position}: a password must be written as text"
        )
    if "password" in entry and "password_hash" in entry:
        raise SourceError(
            f'{where} login {position} names both a "password" and a '
            f'"password_hash"; it can have one'
        )

    # ${VAR} is expanded here so that a missing variable is reported at
    # startup, naming the variable, rather than as a failed login later.
    secret = resolve(written, what=f"{where} login {position}")
    if not secret:
        raise SourceError(f"{where} login {position} has an empty password")
    if hashed:
        # Read once now so a malformed hash is a startup error rather than a
        # refusal nobody can explain.
        _matches_hash(secret, "")
    source = "the environment" if written != secret else "the configuration"
    return Login(user=user, secret=secret, hashed=hashed, source=source)


def from_document(document: dict, where: str) -> LoginStore:
    """The logins a parsed configuration names, which may be none."""
    entries = document.get("logins") or []
    if not isinstance(entries, list):
        raise SourceError(f'{where}: "logins" must be an array')
    logins = [
        _login_from(entry, position, where)
        for position, entry in enumerate(entries, start=1)
    ]
    seen: set[str] = set()
    for login in logins:
        if login.user.lower() in seen:
            raise SourceError(
                f"{where}: two logins are both called '{login.user}'; "
                f"a name is matched without regard to case"
            )
        seen.add(login.user.lower())
    return LoginStore(logins)


def load(config_path: str | Path) -> LoginStore:
    """The logins a configuration file names.

    Read separately from the tables rather than returned beside them: who may
    connect and what they may read are different questions, and the catalog
    has no business holding a password.
    """
    path = Path(config_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"'{path}' is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SourceError(f"'{path}' does not hold a configuration object")
    return from_document(document, f"'{path}'")

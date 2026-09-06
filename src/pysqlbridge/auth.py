"""Windows Authentication, delegated to Windows.

The client's login carries a SPNEGO token, not a bare NTLM message. The
observed blob opened with an ASN.1 application tag wrapping OID 1.3.6.1.5.5.2
and offered four mechanisms, NTLM first, with an optimistic NTLM negotiate
token attached:

    1.3.6.1.4.1.311.2.2.10   NTLM
    1.2.840.48018.1.2.2      Kerberos, the legacy Microsoft OID
    1.2.840.113554.1.2.2     Kerberos 5
    1.3.6.1.4.1.311.2.2.30   NegoEx

Nothing here implements any of that. AcceptSecurityContext does, through
pywin32's Negotiate wrapper, which means Windows performs the exchange and
validates the result against the local account database or the domain. Writing
the NTLM crypto by hand would mean owning a credential check, which is the last
thing this project should own.

Kerberos will not be selected in practice. It needs a service principal name
registered for the bridge's host and port, which requires directory access this
does not have, so SPNEGO falls through to NTLM. That is what the reference
server did when the client reached it by address.
"""

from __future__ import annotations

from dataclasses import dataclass

# SEC_I_CONTINUE_NEEDED. The acceptor returns this while it still expects
# another token from the client, and zero when the context is complete.
SEC_I_CONTINUE_NEEDED = 0x00090312
SEC_E_OK = 0

# Negotiate rather than NTLM: the client sends SPNEGO, so the package that
# understands SPNEGO has to be the one on this side.
PACKAGE = "Negotiate"


class AuthenticationError(Exception):
    """The client could not be authenticated."""


class UnsupportedPlatform(AuthenticationError):
    """Windows Authentication was attempted somewhere that has no SSPI."""


@dataclass
class AuthResult:
    """One step of the exchange."""

    token: bytes
    """What to send back. Empty when there is nothing left to send."""

    complete: bool
    """Whether the client is now authenticated."""


class SspiAcceptor:
    """The server half of a SPNEGO exchange.

    Feed it each token the client sends and return AuthResult.token to the
    client until complete is true.
    """

    def __init__(self) -> None:
        self._auth = _new_server_auth()
        self._complete = False

    @property
    def complete(self) -> bool:
        return self._complete

    def step(self, token: bytes) -> AuthResult:
        """Process one client token."""
        if self._complete:
            raise AuthenticationError("authentication is already complete")
        if not token:
            raise AuthenticationError("client sent an empty authentication token")

        try:
            status, buffers = self._auth.authorize(token)
        except Exception as exc:
            raise AuthenticationError(f"SSPI rejected the token: {exc}") from exc

        outgoing = bytes(buffers[0].Buffer) if buffers else b""

        if status == SEC_E_OK:
            self._complete = True
        elif status != SEC_I_CONTINUE_NEEDED:
            raise AuthenticationError(
                f"SSPI returned an unexpected status 0x{status:08x}"
            )

        return AuthResult(token=outgoing, complete=self._complete)

    @property
    def username(self) -> str | None:
        """Who authenticated, once the exchange has completed.

        Returned as DOMAIN\\user, or MACHINE\\user for a local account.
        """
        if not self._complete:
            return None
        try:
            return self._auth.ctxt.QueryContextAttributes(_NAMES_ATTRIBUTE)
        except Exception:
            # The context is valid whether or not the name can be read back,
            # so a failure here is missing information rather than a failed
            # login, and it is not worth refusing the connection over.
            return None


# SECPKG_ATTR_NAMES
_NAMES_ATTRIBUTE = 1


def _new_server_auth():
    try:
        import sspi
    except ImportError as exc:
        raise UnsupportedPlatform(
            "Windows Authentication needs pywin32, which is Windows only. "
            "Install pysqlbridge on Windows, or use a login path that does "
            "not require SSPI."
        ) from exc
    return sspi.ServerAuth(PACKAGE)

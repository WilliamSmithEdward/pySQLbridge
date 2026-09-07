"""The certificate the bridge presents during the login handshake.

SQL Server generates a self-signed certificate at startup when none is
configured, and its clients accept it, because with encryption negotiated off
the TLS tunnel covers only the login packet and drivers do not validate the
chain for that. This module does the same thing for the same reason.

That acceptance is a property of the client, not a guarantee. A driver
configured to demand encryption for the whole connection will validate, and a
self-signed certificate will fail there. See the caveat in
docs/tds-login-handshake.md.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
import ssl
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

KEY_SIZE = 2048
DEFAULT_VALIDITY_DAYS = 365


@dataclass(frozen=True)
class Certificate:
    """A certificate and its private key, both PEM encoded."""

    cert_pem: bytes
    key_pem: bytes

    @property
    def common_name(self) -> str:
        loaded = x509.load_pem_x509_certificate(self.cert_pem)
        attrs = loaded.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value

    @property
    def not_valid_after(self) -> dt.datetime:
        return x509.load_pem_x509_certificate(self.cert_pem).not_valid_after_utc


def self_signed(
    hostname: str | None = None,
    *,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
) -> Certificate:
    """Generate a self-signed certificate for this host.

    The subject alternative names cover the hostname, localhost and the
    loopback address, because a client that connects by address rather than by
    name still checks the SAN when it checks anything at all.
    """
    hostname = hostname or socket.gethostname()

    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])

    # Backdated so a client whose clock runs slightly behind this one does not
    # reject a certificate generated moments earlier.
    now = dt.datetime.now(dt.timezone.utc)
    not_before = now - dt.timedelta(days=1)

    alt_names: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]

    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(now + dt.timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    return Certificate(
        cert_pem=certificate.public_bytes(serialization.Encoding.PEM),
        key_pem=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def load(cert_path: str | os.PathLike, key_path: str | os.PathLike) -> Certificate:
    """Read a certificate and key already on disk."""
    with open(cert_path, "rb") as handle:
        cert_pem = handle.read()
    with open(key_path, "rb") as handle:
        key_pem = handle.read()
    return Certificate(cert_pem=cert_pem, key_pem=key_pem)


@contextmanager
def _materialised(certificate: Certificate) -> Iterator[tuple[str, str]]:
    """Put the PEMs on disk just long enough to load them.

    ssl.SSLContext.load_cert_chain reads paths and has no in-memory form, so
    the private key has to touch the filesystem. It goes to the per-user temp
    directory and is removed in a finally, including when loading fails.
    """
    paths = []
    try:
        for suffix, payload in ((".crt", certificate.cert_pem),
                                (".key", certificate.key_pem)):
            handle = tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False, prefix="pysqlbridge-"
            )
            try:
                handle.write(payload)
            finally:
                handle.close()
            paths.append(handle.name)
        yield paths[0], paths[1]
    finally:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def server_context(certificate: Certificate) -> ssl.SSLContext:
    """Build the SSLContext the bridge hands to a TLS tunnel.

    Pinned to TLS 1.2, which is a deliberate choice rather than an oversight.
    The reference server negotiated exactly that: its ServerHello carried
    version 0x0303 with no supported_versions extension, the client flight had
    the ChangeCipherSpec that 1.3 does away with, and no session ticket
    appeared anywhere in the capture.

    TLS 1.3 would also break an assumption TDS predates. Its NewSessionTicket
    messages arrive after the handshake reports completion, by which point the
    TDS framing has already stopped, so there is no defined way to carry them:
    framed is wrong because the tunnel is up, bare is wrong because the client
    is not reading bare records yet. Staying on 1.2 keeps the boundary where
    the protocol assumes it is.

    Session tickets are off for the same reason the version is pinned. Python
    offers one by default, which puts a session_ticket extension in the
    ServerHello and a NewSessionTicket message after the Finished; the
    reference server sends neither, and its second flight is 59 bytes against
    the 250 this sent with a ticket in it. Matching the reference server here
    is not cosmetic: the difference was measured through a proxy while
    tracking down a client that completed the whole handshake and login and
    then hung up.

    Client certificates are not requested. SQL Server does not ask for one
    during this handshake, and the client's identity arrives later through
    SSPI instead.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_TICKET
    # The curve SQL Server picks. Python would otherwise choose x25519, which
    # is the better curve and the wrong answer here: this is impersonating a
    # particular server, and the point of the exercise is that a client cannot
    # tell the difference.
    context.set_ecdh_curve("secp384r1")
    with _materialised(certificate) as (cert_path, key_path):
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return context

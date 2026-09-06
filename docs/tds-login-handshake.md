# TDS login handshake, measured

What a real SQL Server does when Excel connects with Windows Authentication.
Everything here was captured off the wire, not read from the protocol
specification, so it reflects what the client and server actually negotiate
by default rather than what they are permitted to negotiate.

## How this was measured

Captured 2026-09-06 on loopback with tshark 4.6.8, npcap loopback adapter,
capture filter `tcp port 1433`.

| Component | Version |
| --- | --- |
| Server | Microsoft SQL Server 2025 (RTM) 17.0.1000.7, Standard Developer Edition |
| Client | MSOLEDBSQL, reporting PRELOGIN version 18.7.5 |
| Driver host | Desktop Excel, driven through pyVBAharness 1.1.1 |
| OS | Windows 11 Pro 10.0.26200 |

The connection used `Provider=MSOLEDBSQL;Data Source=tcp:127.0.0.1,1433;
Initial Catalog=master;Integrated Security=SSPI;`. The `tcp:` prefix is
load-bearing. Without it the provider selects shared memory for a local
instance, no packet reaches the adapter, and the capture comes back empty.

The server confirmed the connection it saw:

```
CONNECTIONPROPERTY('net_transport') = TCP
CONNECTIONPROPERTY('auth_scheme')   = NTLM
SUSER_NAME()                        = <domain>\<user>
```

## The exchange

Twelve TDS packets between the TCP handshake and the first query. Type is the
first byte of the 8-byte TDS packet header.

| # | Direction | Type | Payload | What it is |
| --- | --- | --- | --- | --- |
| 4 | C to S | 18 | 88 | PRELOGIN |
| 6 | S to C | 4 | 48 | PRELOGIN response |
| 8 | C to S | 18 | 155 | TLS Client Hello |
| 10 | S to C | 18 | 1633 | TLS Server Hello, Certificate, Key Exchange, Done |
| 12 | C to S | 18 | 166 | TLS Client Key Exchange, Change Cipher Spec, Finished |
| 14 | S to C | 18 | 59 | TLS Change Cipher Spec, Finished |
| 16 | C to S | (TLS) | 396 | LOGIN7, encrypted, carrying the NTLM negotiate blob |
| 18 | S to C | 4 | 250 | NTLM challenge |
| 20 | C to S | 17 | 129 | NTLM authenticate |
| 22 | S to C | 4 | 40 | Response |
| 24 | S to C | 4 | 467 | LOGINACK token stream |
| 26 | C to S | 1 | 228 | SQL batch |

Three things in that table are worth pulling out, because each one changes
what the emulator has to build.

### The TLS handshake rides inside type 18 packets

Frames 8 through 14 are a normal TLS 1.2 handshake, but every record is
wrapped in a TDS packet whose type byte is 18, the same value PRELOGIN uses.
The emulator cannot hand the socket to a TLS library and walk away. It has to
frame and unframe TDS packets underneath the TLS records for the duration of
the handshake, then stop.

### Encryption is negotiated off and happens anyway

Both sides advertised the same PRELOGIN encryption option:

```
Encryption: Encryption is available but off (0)
```

A TLS handshake followed regardless, the LOGIN7 packet went through it
encrypted (frame 16), and then traffic reverted to cleartext for the rest of
the session. Frames 18 onward dissect as plain TDS.

So "encryption off" means the login is still encrypted and only the login is.
The emulator needs a certificate and a working TLS handshake even in the
configuration that sounds like it does not, and it must drop back to cleartext
immediately afterward or the client will not follow.

### The tunnel is TLS 1.2, and that is worth pinning

The ServerHello reports version 0x0303 and carries no supported_versions
extension. A TLS 1.3 server answering a 1.3-capable client sends that
extension with 0x0304, so its absence settles the question rather than leaving
it to the record layer's compatibility value. Frame 12's content types read
22, 20, 22: handshake, ChangeCipherSpec, handshake, which is the TLS 1.2
client flight. No NewSessionTicket appears anywhere in the capture.

pysqlbridge pins its context to 1.2 for a second reason beyond matching the
reference. TLS 1.3 sends NewSessionTicket after the handshake reports itself
complete, and by that point the TDS framing has already stopped. There is no
correct way to carry those messages: framed is wrong because the tunnel is up,
bare is wrong because the client is not reading bare records yet. TDS predates
the problem and does not answer it. Staying on 1.2 keeps the framing boundary
where the protocol assumes it is.

### Windows Authentication is SPNEGO wrapping NTLM

The mechanism is NTLM, but the wire format is not. Decoding the SSPI field of a
real LOGIN7 shows it opens with an ASN.1 application tag and the OID
1.3.6.1.5.5.2, which is SPNEGO, and offers four mechanisms:

```
1.3.6.1.4.1.311.2.2.10   NTLM         <- first, and the one selected
1.2.840.48018.1.2.2      Kerberos, legacy Microsoft OID
1.2.840.113554.1.2.2     Kerberos 5
1.3.6.1.4.1.311.2.2.30   NegoEx
```

An NTLM negotiate token is attached as SPNEGO's optimistic mechToken, so the
"NTLMSSP" signature is present but 61 bytes into the blob, not at its start.
Code that checked byte 0 for it would find nothing and conclude the client sent
something it did not.

Kerberos is offered and never chosen. It needs a service principal name
registered for the host and port, and the client reached the server by address,
so SPNEGO falls through to NTLM. Getting an SPN would need directory access
this project does not have, which makes NTLM the target and Kerberos out of
scope.

None of this has to be implemented. Windows performs the whole exchange through
SSPI's `AcceptSecurityContext` using the Negotiate package, validating against
the local account database or the domain, so the bridge never handles a
credential. Handing that to the platform is the point: a login server that
implements its own credential check is a login server that gets it wrong.

Note the direction of frame 20: the client's authenticate blob arrives as type
17, a dedicated SSPI message, while both server-side halves come back as type
4 responses. The two directions do not use the same type, and they are not
framed the same way either.

### The two directions are framed differently

Server to client, from frame 18:

```
ed ef 00 a1 81 ec ...
^  ^^^^^ ^^^^^^^^^^^^
|  |     the SPNEGO blob
|  239, little-endian
the SSPI token (0xED)
```

That packet's payload is 242 bytes: the token byte, its two length bytes, and
239 bytes of blob. Note the length is little-endian, while the packet header
wrapping it is big-endian.

Client to server, from frame 20: the payload is the SPNEGO blob raw, starting
straight in at 0xA1. No token byte, no length. The asymmetry exists because
only the server's direction shares a stream with other tokens.

A useful confirmation fell out of this. Windows SSPI, handed the captured
client blob, produced a challenge of exactly 239 bytes, matching the length the
reference server sent down to the byte.

## What the server claims about itself

From the PRELOGIN response (frame 6):

```
Version:    17.0.1000
Encryption: Encryption is available but off (0)
ThreadID:   length 0
MARS:       Off (0)
TraceID:    length 0
Terminator
```

From the LOGINACK token (frame 24):

```
Interface:      1
TDS version:    0x74000004
Server name:    Microsoft SQL Server
Server Version: 17.0.1000
```

TDS 7.4. These are the values the emulator advertises to pass as SQL Server
2025. The client sends its own version, 18.7.5, which is the driver build and
not something the server echoes.

## The full login response

Frame 24 is one TDS response carrying a token stream, in this order:

1. ENVCHANGE, database, `master` to `master`
2. INFO 5701, "Changed database context to 'master'."
3. ENVCHANGE, SQL collation, 5 bytes
4. ENVCHANGE, language, `us_english`
5. INFO 5703, "Changed language setting to us_english."
6. LOGINACK, as above
7. ENVCHANGE, packet size

The INFO tokens carry the server's own host name. Clients display these,
so the emulator should emit the same shape rather than a bare LOGINACK.

## Implementation order this implies

1. TCP accept, TDS packet framing, PRELOGIN request and response.
2. TLS handshake tunneled in type 18 packets, then revert to cleartext.
3. LOGIN7 parse, including the SSPI blob field.
4. NTLM through SSPI `AcceptSecurityContext`, type 4 out, type 17 in.
5. LOGINACK token stream in the order above.
6. SQL batch, type 1, and result sets.

Steps 1 through 5 are all prerequisites to a client ever showing a table list,
so none of them can be deferred behind query work.

## Reproducing

`scripts/capture_login.ps1` starts the capture, drives Excel through
pyVBAharness, and stops cleanly. Two traps cost time and are commented at the
call site: `$args` is a PowerShell automatic variable and needs its own name,
and `Start-Process` does not quote array elements, so a capture filter
containing spaces splits into separate arguments unless it carries its own
quotes.

The capture itself is deliberately not committed. It contains an NTLMv2
challenge and response for a real account, which is offline-crackable against
a weak password. Regenerate it locally rather than sharing one.

## Known gap

Wireshark's TDS dissector marks the result-set response (frame 28) as a
malformed packet. The connection itself succeeded and Excel received correct
values, so this is a dissector limitation rather than a protocol problem. It
does mean the row-encoding side of the protocol will need to be read from the
bytes rather than from the dissection.

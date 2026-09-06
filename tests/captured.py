"""Bytes taken off the wire, used as fixtures.

Captured 2026-09-06 on loopback from desktop Excel (MSOLEDBSQL 18.7.5) against
SQL Server 2025 (17.0.1000.7), Windows Authentication over TCP. Frame numbers
refer to that capture; docs/tds-login-handshake.md walks through it.

Regenerate with scripts/capture_login.ps1, and read the hex out of the pcap
rather than retyping it. These are the real thing rather than hand-written
examples on purpose: a handshake that satisfies the specification but not the
actual server is worth nothing, and the difference between the two is exactly
what this project has to get right.
"""

from binascii import unhexlify

# Frame 4. Client PRELOGIN. Six options, TRACEID carrying 36 bytes.
CLIENT_PRELOGIN = unhexlify(
    "120100580000010000001f000601002500010200260001030027000404002b00"
    "0105002c0024ff1207000500000000483e0000004f9ccf1f076f8c4a8a2c701c"
    "f600230e1141e2d4671440499c86b81a5580359002000000"
)

# Frame 6. Server PRELOGIN response. Same six options, THREADID and TRACEID
# present but empty. This is the message pysqlbridge has to reproduce.
SERVER_PRELOGIN = unhexlify(
    "040100300000010000001f000601002500010200260001030027000004002700"
    "010500280000ff110003e80000000000"
)

# Frame 28's header alone. Its SPID field holds 77, which is the spid the
# server reported for that same session, so this pins the field's position
# and byte order. The rest of that frame is a result set, which nothing
# parses yet.
RESULT_HEADER = unhexlify("0401009c004d0100")

# A real LOGIN7 from .Net SqlClient, captured against this project's own
# listener on 2026-09-06 with scripts/../scratch tooling. 428 bytes.
#
#   HostName   WORKSTATION1        UserName   (empty, Windows auth)
#   AppName    .Net SqlClient Data Provider   Database   master
#   ServerName tcp:127.0.0.1,1337  TDSVersion 0x74000004
#   SSPI       129 bytes of SPNEGO offering NTLM first
#
# Two things are not as captured, both substituted at identical length so
# every offset and length in the message stays valid: ClientID, the six
# bytes holding the network adapter's MAC address, is zeroed, and the
# workstation name is replaced with WORKSTATION1 wherever it appears,
# including inside the NTLM negotiate token. Neither is read by anything,
# and machine identifiers do not belong in a public repository.
CLIENT_LOGIN7 = unhexlify(
    "ac01000004000074401f0000000000062445000000000000e083001000000000"
    "000000005e000c00000000000000000076001c00ae001200d2000400d6001c00"
    "0e0100000e0106000000000000001a0181009b0100009b010000000000005700"
    "4f0052004b00530054004100540049004f004e0031002e004e00650074002000"
    "530071006c0043006c00690065006e0074002000440061007400610020005000"
    "72006f00760069006400650072007400630070003a003100320037002e003000"
    "2e0030002e0031002c0031003300330037009b0100002e004e00650074002000"
    "530071006c0043006c00690065006e0074002000440061007400610020005000"
    "72006f00760069006400650072006d0061007300740065007200607f06062b06"
    "01050502a0753073a030302e060a2b06010401823702020a06092a864882f712"
    "01020206092a864886f712010202060a2b06010401823702021ea23f043d4e54"
    "4c4d535350000100000097b208e209000900340000000c000c00280000000a00"
    "f4650000000f574f524b53544154494f4e31574f524b47524f55500100000000"
    "0401000000020500000000ff"
)


# Frame 24's login response, sliced into its tokens. The nine pieces below
# concatenate back to the whole 459-byte stream with nothing left over, which
# is how the boundaries were established rather than assumed.
ENVCHANGE_DATABASE = unhexlify(
    "e31b0001066d0061007300740065007200066d0061007300740065007200"
)

INFO_5701 = unhexlify(
    "ab700045160000020025004300680061006e0067006500640020006400610074"
    "0061006200610073006500200063006f006e007400650078007400200074006f"
    "00200027006d006100730074006500720027002e000c57004f0052004b005300"
    "54004100540049004f004e0031000001000000"
)

ENVCHANGE_COLLATION = unhexlify(
    "e3080007050904d0003400"
)

ENVCHANGE_LANGUAGE = unhexlify(
    "e31700020a750073005f0065006e0067006c0069007300680000"
)

INFO_5703 = unhexlify(
    "ab740047160000010027004300680061006e0067006500640020006c0061006e"
    "00670075006100670065002000730065007400740069006e006700200074006f"
    "002000750073005f0065006e0067006c006900730068002e000c57004f005200"
    "4b00530054004100540049004f004e0031000001000000"
)

LOGINACK = unhexlify(
    "ad36000174000004164d006900630072006f0073006f00660074002000530051"
    "004c00200053006500720076006500720000000000110003e8"
)

ENVCHANGE_PACKET_SIZE = unhexlify(
    "e3130004043400300039003600043400300039003600"
)

FEATURE_EXT_ACK = unhexlify(
    "ae012e000000000900608114ffe7ffff00020207010401000504ffffffff0601"
    "00070102080800000000000000000904ffffffff090200000002010a01000000"
    "01ff"
)

DONE = unhexlify(
    "fd000000000000000000000000"
)

"""Drive a real Excel instance into a Windows-auth SQL Server login over TCP.

The point is the traffic, not the answer: this runs while tshark captures
loopback 1433, so the resulting pcap holds a genuine TDS handshake produced
by the same OLE DB provider Excel's own SQL Server connector uses.

Data Source must carry the tcp: prefix. Without it the provider picks
shared memory for a local instance and nothing reaches the adapter.
"""

from pyvbaharness import ExcelSession

VBA = r"""
Public Function Probe() As String
    Dim cn As Object, rs As Object
    Set cn = CreateObject("ADODB.Connection")
    cn.ConnectionString = "Provider=MSOLEDBSQL;Data Source=tcp:127.0.0.1,1433;" & _
                          "Initial Catalog=master;Integrated Security=SSPI;"
    cn.Open

    ' Deliberately returns nothing identifying. An earlier version selected
    ' SUSER_NAME(), which put an account name into the capture and therefore
    ' into any fixture cut from it.
    Set rs = cn.Execute("SELECT CONNECTIONPROPERTY('net_transport'), " & _
                        "CONNECTIONPROPERTY('auth_scheme')")
    Probe = rs.Fields(0).Value & " | " & rs.Fields(1).Value
    rs.Close

    ' The reference result set. One column per type the bridge has to encode
    ' first, including a null, chosen so a CSV or JSON source maps onto it.
    ' The long value is here because an API array flattened to JSON routinely
    ' runs past what a sized nvarchar can declare.
    Set rs = cn.Execute("SELECT CAST(42 AS int) AS answer, " & _
                        "CAST('hello' AS nvarchar(20)) AS greeting, " & _
                        "CAST(1.5 AS float) AS ratio, " & _
                        "CAST(NULL AS int) AS missing, " & _
                        "CAST(REPLICATE('ab', 3000) AS nvarchar(max)) AS big")
    Probe = Probe & " | " & rs.Fields(0).Value & "," & rs.Fields(1).Value & _
            "," & rs.Fields(2).Value & ",<null>"
    rs.Close
    cn.Close
End Function
"""


def main() -> int:
    with ExcelSession() as excel:
        result = excel.run_vba(VBA, proc="Probe", timeout=60)
        print("outcome:", result.outcome)
        if result.outcome == "passed":
            print("value  :", result.value)
            return 0
        if result.error is not None:
            print("error  :", result.error.number, result.error.description)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

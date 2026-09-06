"""Advertise arbitrary data sources over the wire as if they were SQL Server."""

__version__ = "0.1.0"

# SQL Server's own port is 1433, and a real instance is usually already on it.
# Listening elsewhere by default keeps the two able to run side by side, which
# matters because the real server is the reference this project is measured
# against. Clients reach the bridge with "tcp:<host>,1337".
DEFAULT_PORT = 1337

"""Advertise arbitrary data sources over the wire as if they were SQL Server."""

def _version() -> str:
    """What version this is, read from the package that was installed.

    Not written here. It was, and it said 0.1.0 while the package on PyPI
    said 1.0.1: two places holding one fact and only one of them kept up.
    pyproject.toml is the one that ships, the one the release workflow
    checks the tag against, and now the only one.

    A source tree with nothing installed has no metadata to read, and says
    so rather than guessing at a number.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("pysqlbridge")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _version()

# SQL Server's own port is 1433, and a real instance is usually already on it.
# Listening elsewhere by default keeps the two able to run side by side, which
# matters because the real server is the reference this project is measured
# against. Clients reach the bridge with "tcp:<host>,1337".
DEFAULT_PORT = 1337

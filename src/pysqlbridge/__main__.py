"""Entry point for `python -m pysqlbridge` and for the packaged executable.

The import is absolute rather than relative on purpose. PyInstaller runs this
as a top-level script with no parent package, so `from .server import ...`
builds an executable that starts and immediately dies with an ImportError.
An absolute import works both frozen and under `python -m`.
"""

from pysqlbridge.server import _main

if __name__ == "__main__":
    _main()

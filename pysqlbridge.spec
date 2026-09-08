# PyInstaller build for a single-file pysqlbridge.exe.
#
# Build with:  pyinstaller pysqlbridge.spec --noconfirm
# Or use scripts/build_exe.ps1, which also smoke-tests the result.

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# sspi is imported inside a function in auth.py, so PyInstaller's static
# analysis never sees it, and neither it nor the modules it reaches at runtime
# get collected. Losing any of them produces an executable that starts, listens
# and loads its tables, then fails the moment a client authenticates. That is
# how win32timezone was found: not by reading the spec, but by running the
# build and watching a login fail with "No module named win32timezone".
WINDOWS_AUTH = [
    "sspi",
    "sspicon",
    "win32security",
    "win32api",
    "pywintypes",
    "win32timezone",   # reached lazily by pywin32 while building a context
]

# cryptography reaches its backend through a compiled module the analysis can
# miss depending on the version installed.
CRYPTO = collect_submodules("cryptography.hazmat.bindings")

analysis = Analysis(
    ["src/pysqlbridge/__main__.py"],
    pathex=["src"],
    # The version is read from the installed metadata rather than written
    # into the source, so the single file has to carry that metadata or it
    # would report itself as unknown.
    datas=copy_metadata("pysqlbridge"),
    hiddenimports=WINDOWS_AUTH + CRYPTO,
    # No tkinter, no test frameworks: this is a headless network service and
    # every megabyte of them is dead weight in the single file.
    excludes=[
        "tkinter", "unittest", "pytest", "_pytest", "pydoc_data",
        "matplotlib", "numpy", "PIL",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="pysqlbridge",
    debug=False,
    upx=False,
    # A console application on purpose. It logs what connects, authenticates
    # and queries, and a windowed build would throw that away.
    console=True,
    disable_windowed_traceback=False,
    strip=False,
)

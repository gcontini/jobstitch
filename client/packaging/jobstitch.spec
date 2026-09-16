# PyInstaller spec for the jobstitch client.
#
#     uv run --with pyinstaller pyinstaller client/packaging/jobstitch.spec
#
# Produces dist/jobstitch (or dist/jobstitch.exe). Build it on the platform
# you are building for — PyInstaller does not cross-compile.
#
# Why PyInstaller and not Cython: Cython compiles modules to C extensions but
# still needs an interpreter and a launcher around them, so it does not
# produce the single file this is for. Nuitka would, and is the fallback if
# start-up time or source opacity ever matters more than build simplicity.

from pathlib import Path

# SPECPATH is where this file lives: <repo>/client/packaging.
CLIENT = Path(SPECPATH).parent
SOURCES = CLIENT / "src"

a = Analysis(
    [str(CLIENT / "packaging" / "entrypoint.py")],
    pathex=[str(SOURCES)],
    binaries=[],
    # The spreadsheet template ships inside the binary; everything else the
    # client needs it reads from beside the binary at runtime.
    datas=[(str(SOURCES / "jobstitch_client" / "resources" / "applications.xlsx"),
            "jobstitch_client/resources")],
    hiddenimports=[],
    # Nothing here belongs in a client: they are the server's dependencies,
    # and excluding them keeps the binary small if one is ever pulled in
    # transitively.
    excludes=["fastapi", "uvicorn", "starlette", "openai", "jinja2", "pypdf",
              "jobstitch_server", "tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jobstitch",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

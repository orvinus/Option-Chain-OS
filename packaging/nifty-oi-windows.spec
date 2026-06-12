# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: single-folder payload in onefile EXE (see scripts/build-windows-exe.ps1)."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

repo = Path(SPECPATH).parent.resolve()
backend = repo / "backend"
frontend_dist = repo / "frontend" / "dist"

cert_datas, cert_binaries, cert_hidden = collect_all("certifi")

hidden = (
    [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "pydantic_settings",
        "psycopg",
        "sqlalchemy.dialects.postgresql",
        "greenlet",
        "orjson",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        "httpx",
        "pyotp",
        "SmartApi",
        "logzero",
        "websocket",
    ]
    + list(cert_hidden)
    + collect_submodules("app")
)

a = Analysis(
    [str(backend / "run.py")],
    pathex=[str(backend)],
    binaries=cert_binaries,
    datas=[
        (str(frontend_dist), "frontend_dist"),
        # Put alembic.ini inside "bundle/" (basename preserved); avoids one-file unpack edge cases.
        (str(backend / "alembic.ini"), "bundle"),
        (str(backend / "alembic"), "bundle/alembic"),
    ]
    + list(cert_datas),
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="NiftyOI-Analytics",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all


datas = [
    ("frontend", "frontend"),
    ("config", "config"),
    ("data", "data"),
]
binaries = []
hiddenimports = []

# These frameworks use plugins/dynamic imports which static analysis cannot
# always discover. collect_all keeps the packaged build functionally aligned
# with the existing Python environment.
for package in (
    "uvicorn",
    "webview",
    "langchain",
    "langchain_core",
    "langchain_community",
    "langgraph",
    "llama_index",
    "weaviate",
):
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hiddenimports
    except Exception:
        # Optional providers not installed in the build environment should not
        # prevent PyInstaller from producing a diagnostic build.
        pass

a = Analysis(
    ["desktop_launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="InterviewAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="InterviewAgent",
)

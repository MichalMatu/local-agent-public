# PyInstaller-only macOS packaging prototype; NOT a signed distribution.
from pathlib import Path

root = Path(SPECPATH).resolve().parents[3]
a = Analysis(
    [str(root / "scripts" / "desktop_menu_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalAgentMenu",
    console=False,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="LocalAgentMenu")
app = BUNDLE(
    coll,
    name="Local Agent Menu.app",
    icon=None,
    bundle_identifier="local.localagent.menu.preview",
    info_plist={
        "LSUIElement": True,
        "CFBundleName": "Local Agent Menu",
        "CFBundleDisplayName": "Local Agent Menu",
        "NSHighResolutionCapable": True,
    },
)

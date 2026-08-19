# PyInstaller spec for the standalone jaigent binary.
#
# Produces a single self-contained executable that needs no Python:
#     jaigent.exe   on Windows
#     jaigent       on macOS and Linux
#
# Build it with:
#     pip install pyinstaller
#     pyinstaller packaging/jaigent.spec --clean --noconfirm
#
# The result lands in dist/. CI builds one per platform and attaches them to
# the GitHub release.

import sys
from pathlib import Path

# `.spec` files are exec'd, so __file__ is not defined; SPECPATH is.
ROOT = Path(SPECPATH).parent  # noqa: F821
IS_WINDOWS = sys.platform.startswith("win")

# Windows executables carry an .ico resource. PyInstaller aborts the whole
# build if the file it is pointed at is missing, so check rather than assume:
# a binary with a default icon beats no binary at all.
ICON_FILE = ROOT / "packaging" / "icon.ico"
ICON = str(ICON_FILE) if IS_WINDOWS and ICON_FILE.is_file() else None

block_cipher = None

# Every unicode table rich ships. Cheap to include and the alternative is a
# binary that crashes the moment it renders the logo.
_RICH_UNICODE_TABLES = [
    "_versions",
    "unicode4-1-0",
    "unicode5-0-0",
    "unicode5-1-0",
    "unicode5-2-0",
    "unicode6-0-0",
    "unicode6-1-0",
    "unicode6-2-0",
    "unicode6-3-0",
    "unicode7-0-0",
    "unicode8-0-0",
    "unicode9-0-0",
    "unicode10-0-0",
    "unicode11-0-0",
    "unicode12-0-0",
    "unicode12-1-0",
    "unicode13-0-0",
    "unicode14-0-0",
    "unicode15-0-0",
    "unicode15-1-0",
    "unicode16-0-0",
    "unicode17-0-0",
]

analysis = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "src" / "jaigent" / "data"), "jaigent/data"),
    ],
    hiddenimports=[
        # Imported lazily or by name, so PyInstaller cannot see them statically.
        "jaigent.llm.openai",
        "jaigent.llm.anthropic",
        "jaigent.llm.gemini",
        "jaigent.mcp",
        "jaigent.plugins",
        "jaigent.memory",
        "jaigent.tools.files",
        "jaigent.tools.web",
        "jaigent.tools.shell",
        "jaigent.checkpoint",
        "jaigent.failover",
        "jaigent.gateway",
        "jaigent.router",
        "jaigent.skills",
        "jaigent.commands",
        "jaigent.schedule",
        "jaigent.settings_store",
        "jaigent.updater",
        # rich picks its unicode width table at runtime by building the module
        # name from the Unicode version, so no static analysis can find these.
        # Missing them means the binary dies the first time it measures a wide
        # character -- which the logo does, immediately.
        *[f"rich._unicode_data.{name}" for name in _RICH_UNICODE_TABLES],
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the binary small: none of these are used at runtime.
        "tkinter",
        "unittest",
        "pytest",
        "mypy",
        "ruff",
        "setuptools",
        "pip",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="jaigent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # jaigent is a terminal application
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

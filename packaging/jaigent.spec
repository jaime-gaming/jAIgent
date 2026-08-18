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

block_cipher = None

analysis = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Imported lazily or by name, so PyInstaller cannot see them statically.
        "jaigent.llm.openai",
        "jaigent.llm.anthropic",
        "jaigent.llm.gemini",
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
    icon=str(ROOT / "packaging" / "icon.ico") if IS_WINDOWS else None,
)

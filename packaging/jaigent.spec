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

import re
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

# `.spec` files are exec'd, so __file__ is not defined; SPECPATH is.
ROOT = Path(SPECPATH).parent  # noqa: F821
IS_WINDOWS = sys.platform.startswith("win")

# Windows executables carry an .ico resource. PyInstaller aborts the whole
# build if the file it is pointed at is missing, so check rather than assume:
# a binary with a default icon beats no binary at all.
ICON_FILE = ROOT / "packaging" / "icon.ico"
ICON = str(ICON_FILE) if IS_WINDOWS and ICON_FILE.is_file() else None

# Publisher / version metadata so the Windows exe shows an Editor (CompanyName)
# instead of \"Unknown publisher\" in Defender / SmartScreen dialogs and in the
# file-properties Details tab. Without this PyInstaller leaves the version
# resource empty and Windows classifies the download as untrusted.
_VERSION = "0.5.6"
_VERSION_TUPLE = (0, 5, 5, 0)
try:
    _init_text = (ROOT / "src" / "jaigent" / "__init__.py").read_text(encoding="utf-8")
    _m = re.search(r'__version__\s*=\s*"([^"]+)"', _init_text)
    if _m:
        _VERSION = _m.group(1).strip()
        _parts = _VERSION.split(".")
        _nums: list[int] = []
        for p in _parts:
            digits = "".join(ch for ch in p if ch.isdigit())
            if digits == "":
                break
            _nums.append(int(digits))
            if len(_nums) == 4:
                break
        while len(_nums) < 4:
            _nums.append(0)
        _VERSION_TUPLE = tuple(_nums[:4])  # type: ignore[assignment]
except Exception:
    pass

_VERSION_FILE: str | None = None
if IS_WINDOWS:
    # Built at spec-exec time so the version never drifts from src/jaigent/__init__.py.
    # Written under build/ so --clean still leaves a fresh copy for this run.
    _version_src = (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={_VERSION_TUPLE},\n"
        f"    prodvers={_VERSION_TUPLE},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "    ),\n"
        "  kids=[\n"
        "    StringFileInfo(\n"
        "      [\n"
        "      StringTable(\n"
        "        u'040904B0',\n"
        "        [StringStruct(u'CompanyName', u'jaime-gaming'),\n"
        "         StringStruct(u'FileDescription', u'jaigent - All your agents in one place'),\n"
        f"         StringStruct(u'FileVersion', u'{_VERSION}'),\n"
        "         StringStruct(u'InternalName', u'jaigent'),\n"
        "         StringStruct(u'LegalCopyright', u'© 2026 jaime-gaming'),\n"
        "         StringStruct(u'OriginalFilename', u'jaigent.exe'),\n"
        "         StringStruct(u'ProductName', u'jaigent'),\n"
        f"         StringStruct(u'ProductVersion', u'{_VERSION}')])\n"
        "      ]),\n"
        "    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )
    try:
        _version_path = ROOT / "build" / "version_info.txt"
        _version_path.parent.mkdir(parents=True, exist_ok=True)
        _version_path.write_text(_version_src, encoding="utf-8")
        _VERSION_FILE = str(_version_path)
    except Exception:
        _VERSION_FILE = None

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
        *collect_submodules("jaigent"),
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
    noarchive=True,
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # jaigent is a terminal application
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=_VERSION_FILE,
    # Explicit manifest so Windows knows this is DPI-aware / long-path-aware and
    # runs asInvoker without a UAC heuristic that flags the binary as installer.
    manifest='<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0"><assemblyIdentity version="0.5.6.0" name="jaigent" type="win32" processorArchitecture="*"/><dependency><dependentAssembly><assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/></dependentAssembly></dependency><compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application><supportedOS Id="{e2011457-1546-43c5-a5fe-008deee3d3f0}"/><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application></compatibility></assembly>',
)

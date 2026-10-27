# -*- mode: python ; coding: utf-8 -*-
# 单一发行版（含 WebEngine 网页模式）· onedir：产物 dist/CtrlTranslate/CtrlTranslate.exe
# onefile → onedir：220MB 单 exe 每次启动解压到 %TEMP% 既慢又是杀软扫描/隔离
# 重灾区（QtWebEngineProcess 被 UPX 压缩 + 解压后被隔离 = 网页功能整块失灵）
# 网页引擎是应用内模式开关（webai.enabled），不再拆 lite/Web 双变体
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['comtypes.stream']
hiddenimports += collect_submodules('edge_tts')

# 版本资源（Windows VERSIONINFO）：SignPath 代码签名要求签名的二进制带一致的
# 产品名/版本元数据。版本号从 app/__init__.py 正则提取（不 import，避免副作用），
# 与 release.yml 的 version gate 同源，杜绝 spec 与代码版本漂移。
import re as _re
from PyInstaller.utils.win32 import versioninfo as _vi

with open('app/__init__.py', encoding='utf-8') as _f:
    _m = _re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", _f.read())
assert _m, "app/__init__.py 中未找到 __version__"
APP_VERSION = _m.group(1)

# FixedFileInfo 要求 4 段整型版本（Windows 惯例不足补 .0，如 1.6.1 → 1.6.1.0）
_v = [int(x) for x in (APP_VERSION.split('.') + ['0'])[:4]]

version_info = _vi.VSVersionInfo(
    ffi=_vi.FixedFileInfo(
        filevers=tuple(_v), prodvers=tuple(_v),
        mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0),
    ),
    kids=[
        _vi.StringFileInfo([
            _vi.StringTable('040904B0', [
                _vi.StringStruct('CompanyName', 'techdou'),
                _vi.StringStruct('FileDescription', 'CtrlTranslate - 划词/截图/网页 AI 翻译工具'),
                _vi.StringStruct('FileVersion', '.'.join(str(x) for x in _v)),
                _vi.StringStruct('InternalName', 'CtrlTranslate'),
                _vi.StringStruct('LegalCopyright', 'Copyright (c) 2026 techdou (MIT License)'),
                _vi.StringStruct('OriginalFilename', 'CtrlTranslate.exe'),
                _vi.StringStruct('ProductName', 'CtrlTranslate'),
                _vi.StringStruct('ProductVersion', APP_VERSION),
            ])
        ]),
        _vi.VarFileInfo([_vi.VarStruct('Translation', [1033, 1200])]),
    ],
)


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/icon.png', 'assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CtrlTranslate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    version=version_info,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CtrlTranslate',
)

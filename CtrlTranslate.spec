# -*- mode: python ; coding: utf-8 -*-
# 单一发行版（含 WebEngine 网页模式）· onedir：产物 dist/CtrlTranslate/CtrlTranslate.exe
# onefile → onedir：220MB 单 exe 每次启动解压到 %TEMP% 既慢又是杀软扫描/隔离
# 重灾区（QtWebEngineProcess 被 UPX 压缩 + 解压后被隔离 = 网页功能整块失灵）
# 网页引擎是应用内模式开关（webai.enabled），不再拆 lite/Web 双变体
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['comtypes.stream']
hiddenimports += collect_submodules('edge_tts')


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

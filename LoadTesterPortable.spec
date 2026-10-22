# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['loadtester.py'],
    pathex=[],
    binaries=[('C:\\Users\\yahowa\\AppData\\Local\\Programs\\Python\\Python310\\DLLs\\_tkinter.pyd', '.'), ('C:\\Users\\yahowa\\AppData\\Local\\Programs\\Python\\Python310\\DLLs\\tcl86t.dll', '.'), ('C:\\Users\\yahowa\\AppData\\Local\\Programs\\Python\\Python310\\DLLs\\tk86t.dll', '.')],
    datas=[('C:\\Users\\yahowa\\AppData\\Local\\Programs\\Python\\Python310\\Lib\\tkinter', 'tkinter'), ('C:\\Users\\yahowa\\AppData\\Local\\Programs\\Python\\Python310\\tcl\\tcl8.6', 'tcl\\tcl8.6'), ('C:\\Users\\yahowa\\AppData\\Local\\Programs\\Python\\Python310\\tcl\\tk8.6', 'tcl\\tk8.6')],
    hiddenimports=['_tkinter'],
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
    a.binaries,
    a.datas,
    [],
    name='LoadTesterPortable',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

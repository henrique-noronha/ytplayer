# -*- mode: python ; coding: utf-8 -*-
# yt-dlp compilado a partir deste mesmo ambiente Python — garante que o yt-dlp-ejs
# (resolve o desafio JS do YouTube) vai embutido, sem depender de instalação separada.
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
for pkg in ('yt_dlp', 'yt_dlp_ejs'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['ytdlp_cli.py'],
    pathex=['.'],
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
    a.binaries,
    a.datas,
    [],
    name='YTPlayer-ytdlp',
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

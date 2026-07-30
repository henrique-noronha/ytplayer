"""Wrapper para empacotar o yt-dlp (com yt-dlp-ejs) como executável próprio via PyInstaller.

O daemon do MPV aponta para o .exe gerado a partir daqui (ytdl_hook-ytdl_path),
em vez de depender de uma instalação separada de yt-dlp no sistema.
"""

import sys

import yt_dlp

if __name__ == "__main__":
    sys.exit(yt_dlp.main())

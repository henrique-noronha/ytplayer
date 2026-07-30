@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ==========================================
echo  YTPlayer -- Build de Distribuicao
echo ==========================================
echo.

:: Verifica PyInstaller
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Instalando PyInstaller...
    pip install pyinstaller
    if errorlevel 1 ( echo ERRO: falha ao instalar PyInstaller & exit /b 1 )
)

cd backend

:: Binarios externos obrigatorios (nao ficam no git - ver .gitignore)
if not exist "libmpv-2.dll" (
    echo ERRO: backend\libmpv-2.dll nao encontrado.
    echo Copie o arquivo para backend\ antes de buildar.
    cd .. & pause & exit /b 1
)
if not exist "node.exe" (
    echo ERRO: backend\node.exe nao encontrado.
    echo Copie o node.exe portatil ^(ex: de C:\Program Files\nodejs\node.exe^) para backend\ antes de buildar.
    cd .. & pause & exit /b 1
)

:: Limpa builds anteriores
if exist "build" rmdir /s /q "build"
if exist "dist"  rmdir /s /q "dist"

echo [1/4] Compilando yt-dlp proprio (YTPlayer-ytdlp.exe)...
python -m PyInstaller --noconfirm YTPlayer-ytdlp.spec
if errorlevel 1 ( echo. & echo ERRO ao compilar yt-dlp & cd .. & pause & exit /b 1 )

echo.
echo [2/4] Compilando daemon do MPV (YTPlayer-daemon.exe)...
python -m PyInstaller --noconfirm YTPlayer-daemon.spec
if errorlevel 1 ( echo. & echo ERRO ao compilar daemon & cd .. & pause & exit /b 1 )

echo.
echo [3/4] Compilando interface principal (YTPlayer.exe)...
python -m PyInstaller --noconfirm YTPlayer.spec
if errorlevel 1 ( echo. & echo ERRO ao compilar interface & cd .. & pause & exit /b 1 )

echo.
echo [4/4] Montando pasta final...

:: Copia o daemon, o yt-dlp proprio e o node.exe para dentro da pasta do app principal
copy /y "dist\YTPlayer-daemon.exe" "dist\YTPlayer\" >nul
copy /y "dist\YTPlayer-ytdlp.exe"  "dist\YTPlayer\" >nul
copy /y "node.exe"                "dist\YTPlayer\" >nul

cd ..

echo.
echo ==========================================
echo  Pronto!
echo  Pasta gerada: backend\dist\YTPlayer\
echo.
echo  Para instalar em outro computador:
echo  1. Copie a pasta YTPlayer\ inteira
echo  2. Coloque um cookies.txt na mesma pasta do YTPlayer.exe
echo     (exportado de uma conta logada no YouTube, ex: extensao
echo     "Get cookies.txt LOCALLY")
echo  3. Execute YTPlayer.exe
echo  (sem precisar instalar Python, Node.js, ou nenhuma outra dependencia -
echo   o instalador do .NET/WebView2 roda sozinho na primeira execucao, se faltar)
echo ==========================================
pause

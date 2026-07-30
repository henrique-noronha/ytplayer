"""Ponto de entrada do YTPlayer — FastAPI + PyWebView."""

import asyncio
import json
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.player import Player

PORT = 18100

if getattr(sys, "frozen", False):
    # --onedir: os dados empacotados (--add-data) ficam em sys._MEIPASS; arquivos
    # externos/por instalação (cookies.txt, node.exe) ficam ao lado do .exe (sys.executable).
    FRONTEND_DIR = Path(sys._MEIPASS) / "frontend"
    _DATA_DIR = Path(sys.executable).parent
else:
    FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
    _DATA_DIR = Path(__file__).parent

# Mesmo cookies.txt usado pelo mpv_daemon.py — evita o bloqueio "Sign in to confirm
# you're not a bot" do YouTube nas consultas de qualidade disponível.
COOKIES_PATH = _DATA_DIR / "cookies.txt"

# node.exe empacotado junto (sem depender de instalação separada no sistema) — o
# yt-dlp usa isso para resolver o desafio JS (nsig) do YouTube.
NODE_EXE_PATH = _DATA_DIR / "node.exe"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# Gerenciador de conexões WebSocket

class ConnectionManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)
        logger.info("WS conectado — total: %d", len(self._clients))

    def disconnect(self, ws: WebSocket):
        if ws in self._clients:
            self._clients.remove(ws)
        logger.info("WS desconectado — total: %d", len(self._clients))

    async def broadcast(self, data: dict):
        payload = json.dumps(data, ensure_ascii=False)
        dead = []
        for client in self._clients:
            try:
                await client.send_text(payload)
            except Exception:
                dead.append(client)
        for c in dead:
            self._clients.remove(c)


manager = ConnectionManager()
current_url: Optional[str] = None
player_instance: Optional[Player] = None
last_status: dict = {
    "event": "status", "playing": False, "paused": False,
    "position": 0.0, "duration": None, "title": None, "volume": 100.0, "quality": "auto",
}

# Reconexão automática quando uma live cai (reason == "error")
RECONNECT_DELAYS = [2, 4, 8, 15, 30]
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_RESET_AFTER = 60  # segundos estável antes de zerar o contador (evita reset prematuro)
reconnect_attempts = 0
reconnect_timer: Optional[threading.Timer] = None
last_error_at: Optional[float] = None
current_is_live = False


def _cancel_reconnect():
    global reconnect_timer, reconnect_attempts, last_error_at
    if reconnect_timer:
        reconnect_timer.cancel()
        reconnect_timer = None
    reconnect_attempts = 0
    last_error_at = None
    if player_instance:
        player_instance.hide_standby()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global player_instance
    loop = asyncio.get_event_loop()

    def _on_status(msg: dict):
        last_status.update(msg)
        asyncio.run_coroutine_threadsafe(manager.broadcast(msg), loop)

    def _on_end_file(reason: str):
        global reconnect_attempts, reconnect_timer, last_error_at

        if reason == "stop":
            _cancel_reconnect()
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"event": "end-file", "reason": reason}), loop
            )
            return

        if reason == "error":
            now = time.monotonic()
            # Se a última falha foi há muito tempo, trata como um incidente novo (zera o contador)
            if last_error_at is not None and (now - last_error_at) > RECONNECT_RESET_AFTER:
                reconnect_attempts = 0
            last_error_at = now

            if current_url and reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
                reconnect_attempts += 1
                delay = RECONNECT_DELAYS[min(reconnect_attempts - 1, len(RECONNECT_DELAYS) - 1)]
                logger.warning(
                    "Live caiu — tentativa de reconexão %d/%d em %ds: %s",
                    reconnect_attempts, MAX_RECONNECT_ATTEMPTS, delay, current_url,
                )
                asyncio.run_coroutine_threadsafe(manager.broadcast({
                    "event": "reconnecting",
                    "attempt": reconnect_attempts,
                    "max_attempts": MAX_RECONNECT_ATTEMPTS,
                    "delay": delay,
                }), loop)
                player_instance.show_standby()

                def _do_reconnect(url=current_url):
                    player_instance.hide_standby()
                    player_instance.play(url, is_live=current_is_live)

                reconnect_timer = threading.Timer(delay, _do_reconnect)
                reconnect_timer.daemon = True
                reconnect_timer.start()
            elif reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.error("Reconexão falhou após %d tentativas — desistindo", reconnect_attempts)
                player_instance.show_standby()
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({"event": "reconnect_failed"}), loop
                )
        else:
            reconnect_attempts = 0
            last_error_at = None

        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"event": "end-file", "reason": reason}), loop
        )

    player_instance = Player(on_status=_on_status, on_end_file=_on_end_file)
    logger.info("YTPlayer iniciado")
    yield

    player_instance.shutdown()
    logger.info("YTPlayer encerrado")


app = FastAPI(title="YTPlayer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# Descoberta das qualidades (alturas) realmente disponíveis e se a URL é uma live.
#
# O cliente padrão do yt-dlp dá a melhor qualidade para vídeos normais, mas algumas
# lives só resolvem via o cliente "mweb" (mobile web) — por isso tenta o padrão
# primeiro e só cai para mweb se o padrão falhar ou não retornar formatos.

def _extract_info(url: str, extra_opts: Optional[dict] = None) -> Optional[dict]:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 8,
        "js_runtimes": {"node": {"path": str(NODE_EXE_PATH)}},
    }
    if COOKIES_PATH.is_file():
        opts["cookiefile"] = str(COOKIES_PATH)
    if extra_opts:
        opts.update(extra_opts)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else {}
    return info


def _probe_stream_info(url: str) -> dict:
    info = None
    try:
        info = _extract_info(url)
    except Exception as exc:
        logger.info("Extração padrão falhou para %s (%s) — tentando cliente mweb", url, exc)

    if not info or not info.get("formats"):
        try:
            info = _extract_info(url, {"extractor_args": {"youtube": {"player_client": ["mweb"]}}})
        except Exception as exc:
            logger.warning("Falha ao consultar informações de %s: %s", url, exc)
            return {"heights": [], "is_live": False}

    formats = (info or {}).get("formats") or []
    heights = {
        f["height"] for f in formats
        if f.get("height") and f.get("vcodec") not in (None, "none")
    }
    return {"heights": sorted(heights, reverse=True), "is_live": bool((info or {}).get("is_live"))}


async def _probe_and_prepare(url: str) -> bool:
    """Consulta qualidades/is_live, atualiza current_is_live e transmite o resultado.

    Retorna is_live para o chamador usar imediatamente no player.play().
    """
    global current_is_live
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _probe_stream_info, url)
    current_is_live = info["is_live"]
    await manager.broadcast({"event": "available_qualities", "url": url, "heights": info["heights"]})
    return current_is_live


async def _handle_command(cmd: dict):
    global current_url
    action = cmd.get("action")
    logger.info("Comando recebido: %s", action)

    if action == "play":
        url = (cmd.get("url") or "").strip()
        if url:
            _cancel_reconnect()
            current_url = url
            is_live = await _probe_and_prepare(url)
            player_instance.play(url, is_live=is_live)

    elif action == "toggle_pause":
        if last_status.get("paused"):
            player_instance.resume()
        else:
            player_instance.pause()

    elif action == "stop":
        _cancel_reconnect()
        player_instance.stop()

    elif action == "seek":
        player_instance.seek(float(cmd.get("seconds", 0.0)), cmd.get("mode", "absolute"))

    elif action == "set_volume":
        vol = max(0.0, min(200.0, float(cmd.get("volume", 100.0))))
        player_instance.set_volume(vol)

    elif action == "set_quality":
        player_instance.set_quality(str(cmd.get("quality", "auto")))

    else:
        logger.warning("Ação desconhecida: %s", action)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps(last_status))
        while True:
            raw = await ws.receive_text()
            await _handle_command(json.loads(raw))
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as exc:
        logger.error("Erro no WS: %s", exc)
        manager.disconnect(ws)


@app.get("/api/ping")
async def ping():
    return {"ytplayer": True}


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


def _run_server():
    uvicorn.run(app, host="0.0.0.0", port=PORT, reload=False, log_level="info", log_config=None)


def _wait_and_navigate(window):
    import socket
    for _ in range(60):
        time.sleep(0.5)
        try:
            s = socket.create_connection(("localhost", PORT), timeout=1)
            s.close()
            window.load_url(f"http://localhost:{PORT}")
            return
        except OSError:
            pass


def _on_closing() -> bool | None:
    """Pergunta se deve encerrar só a interface ou interface + player (MPV).

    Retornar False cancela o fechamento da janela (API do pywebview).
    """
    import ctypes

    MB_YESNOCANCEL = 0x03
    MB_ICONQUESTION = 0x20
    IDYES, IDCANCEL = 6, 2

    result = ctypes.windll.user32.MessageBoxW(
        0,
        "O que você deseja fazer?\n\n"
        "SIM — encerrar a interface E o player (MPV)\n"
        "NÃO — encerrar somente a interface (o player continua rodando em segundo plano)\n"
        "CANCELAR — não fechar",
        "YTPlayer — Encerrar",
        MB_YESNOCANCEL | MB_ICONQUESTION,
    )

    if result == IDCANCEL:
        return False

    if result == IDYES and player_instance:
        player_instance.quit_daemon()

    return None


def _check_dependencies():
    """Verifica .NET Framework 4.8+ e WebView2 Runtime (exigidos pelo PyWebView no
    Windows) e instala automaticamente se estiverem faltando, para que o YTPlayer
    rode sem precisar de nenhuma instalação manual prévia."""
    import ctypes
    import subprocess
    import tempfile
    import urllib.request
    import winreg
    from pathlib import Path as _Path

    MB_OK, MB_OKCANCEL = 0x00, 0x01
    MB_ICONERROR, MB_ICONWARN, MB_ICONINFO = 0x10, 0x30, 0x40
    IDOK = 1

    def _msgbox(msg, title="YTPlayer", style=MB_OK | MB_ICONINFO):
        return ctypes.windll.user32.MessageBoxW(0, msg, title, style)

    def _is_admin():
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def _dotnet_ok():
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
            )
            val, _ = winreg.QueryValueEx(key, "Release")
            winreg.CloseKey(key)
            return val >= 528040  # .NET 4.8+
        except Exception:
            return False

    def _webview2_ok():
        guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (
                rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}",
                rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}",
            ):
                try:
                    winreg.OpenKey(hive, sub)
                    return True
                except Exception:
                    pass
        return False

    missing = []
    if not _dotnet_ok():
        missing.append((
            ".NET Framework 4.8",
            "https://go.microsoft.com/fwlink/?LinkId=2085155",
            "ndp48-web.exe",
            ["/passive", "/norestart"],
        ))
    if not _webview2_ok():
        missing.append((
            "WebView2 Runtime",
            "https://go.microsoft.com/fwlink/p/?LinkId=2124703",
            "webview2setup.exe",
            ["/silent", "/install"],
        ))

    if not missing:
        return

    if not _is_admin():
        _msgbox(
            "O YTPlayer precisa instalar componentes obrigatórios da Microsoft.\n\n"
            "Clique em OK para continuar — o Windows pedirá permissão de administrador.",
            "YTPlayer — Permissão necessária",
            MB_OK | MB_ICONWARN,
        )
        params = " ".join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit(0)

    names = "\n".join(f"• {n}" for n, *_ in missing)
    res = _msgbox(
        f"O YTPlayer precisa dos seguintes componentes para funcionar:\n\n{names}"
        f"\n\nTodos são gratuitos e fornecidos pela Microsoft."
        f"\n\nClique em OK para instalar automaticamente agora.",
        "YTPlayer — Instalação necessária",
        MB_OKCANCEL | MB_ICONWARN,
    )
    if res != IDOK:
        sys.exit(0)

    tmp = _Path(tempfile.gettempdir())
    errors = []
    needs_reboot = False

    for name, url, fname, flags in missing:
        dest = tmp / fname
        try:
            urllib.request.urlretrieve(url, dest)
            proc = subprocess.run([str(dest)] + flags)
            if proc.returncode in (1641, 3010):
                needs_reboot = True
            elif proc.returncode != 0:
                errors.append(f"{name}: código {proc.returncode}")
        except Exception as e:
            errors.append(f"{name}: {e}")

    if errors:
        _msgbox(
            "Ocorreu um erro durante a instalação:\n\n" + "\n".join(errors)
            + "\n\nTente instalar manualmente pelo site da Microsoft.",
            "YTPlayer — Erro na instalação",
            MB_OK | MB_ICONERROR,
        )
        sys.exit(1)

    if needs_reboot:
        _msgbox(
            "Instalação concluída!\n\nReinicie o Windows para finalizar a instalação do .NET Framework"
            " e depois abra o YTPlayer novamente.",
            "YTPlayer — Reinicialização necessária",
            MB_OK | MB_ICONINFO,
        )
        sys.exit(0)

    _msgbox(
        "Instalação concluída!\n\nAbra o YTPlayer novamente para usar.",
        "YTPlayer",
        MB_OK | MB_ICONINFO,
    )
    sys.exit(0)


if __name__ == "__main__":
    _check_dependencies()

    import webview

    _SPLASH = """<!DOCTYPE html><html><head><style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f1117;display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:100vh;gap:24px;
  font-family:'Segoe UI',system-ui,sans-serif;color:#e5e7eb}
.logo{font-size:28px;font-weight:700;letter-spacing:.02em}
.logo span{color:#4f8ef7}
.loader{display:flex;gap:8px}
.dot{width:9px;height:9px;border-radius:50%;background:#4f8ef7;
  box-shadow:0 0 8px rgba(79,142,247,.5);animation:wave 1.4s ease infinite}
.dot:nth-child(2){animation-delay:.18s}
.dot:nth-child(3){animation-delay:.36s}
.dot:nth-child(4){animation-delay:.54s}
@keyframes wave{0%,100%{transform:translateY(0);opacity:1}50%{transform:translateY(-8px);opacity:.3}}
.lbl{font-size:11px;color:#6b7280;letter-spacing:.1em;text-transform:uppercase}
</style></head><body>
<div class="logo">YT<span>Player</span></div>
<div class="loader"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
<span class="lbl">Iniciando servidor…</span>
</body></html>"""

    threading.Thread(target=_run_server, daemon=True).start()

    window = webview.create_window(
        "YTPlayer",
        html=_SPLASH,
        width=1100,
        height=760,
        min_size=(760, 560),
    )
    window.events.closing += _on_closing
    webview.start(lambda: threading.Thread(target=_wait_and_navigate, args=(window,), daemon=True).start())

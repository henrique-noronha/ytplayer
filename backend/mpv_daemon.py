"""Daemon MPV do YTPlayer — processo independente, TCP 127.0.0.1:6601.

Sobrevive a recargas da interface e a quedas do servidor FastAPI: o vídeo
continua tocando enquanto o daemon estiver de pé. Protocolo JSON delimitado
por linha (newline-delimited JSON).

Comandos aceitos:
    {"action": "play",       "url": "https://youtube.com/watch?v=..."}
    {"action": "pause"}   |  {"action": "resume"}  |  {"action": "stop"}
    {"action": "seek",       "seconds": 30.0, "mode": "absolute"|"relative"}
    {"action": "set_volume", "volume": 100}   -- 0-200 (100 = 0 dB, 200 = +6 dB)
    {"action": "set_quality","quality": "auto"|"<altura em px, ex: '1080'>"}
    {"action": "get_state"}
    {"action": "show_standby", "message": "Voltaremos em breve..."}  -- tela de espera (imagem + texto)
    {"action": "hide_standby"}
    {"action": "quit_daemon"}  -- encerra o MPV e finaliza o processo do daemon (não sobrevive a este)

Eventos emitidos:
    {"event": "status", "playing": bool, "paused": bool, "position": float,
     "duration": float|None, "title": str|None, "volume": float, "quality": str}
    {"event": "end-file",  "reason": "eof"|"stop"|"error"}
    {"event": "mpv_closed"}
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

if getattr(sys, "frozen", False):
    # --onefile: os binários empacotados (libmpv-2.dll) são extraídos em tempo de
    # execução para sys._MEIPASS. Arquivos externos/por instalação (cookies.txt,
    # images/, daemon.log) ficam na pasta real ao lado do .exe (sys.executable),
    # já que essa pasta é a mesma da distribuição do YTPlayer.exe principal.
    _BACKEND_DIR = str(Path(sys._MEIPASS))
    _DATA_DIR = Path(sys.executable).parent
    IMAGES_DIR = _DATA_DIR / "images"
else:
    _BACKEND_DIR = str(Path(__file__).parent)
    _DATA_DIR = Path(__file__).parent
    IMAGES_DIR = _DATA_DIR.parent / "images"

# Garante que libmpv-2.dll seja encontrada pelo python-mpv
os.environ["PATH"] = _BACKEND_DIR + os.pathsep + os.environ.get("PATH", "")

HOST = "127.0.0.1"
PORT = 6601

# Cookies exportados de uma conta logada no YouTube (ex: "Get cookies.txt LOCALLY"),
# usados para evitar o bloqueio "Sign in to confirm you're not a bot" do YouTube.
COOKIES_PATH = _DATA_DIR / "cookies.txt"

# Binários próprios (empacotados junto, sem depender de instalação separada no sistema):
# node.exe (runtime JS que o yt-dlp usa para resolver o desafio "n" do YouTube) e o
# yt-dlp compilado a partir deste mesmo projeto (garante o yt-dlp-ejs embutido).
NODE_EXE_PATH = _DATA_DIR / "node.exe"
YTDLP_EXE_PATH = _DATA_DIR / "YTPlayer-ytdlp.exe"

# Tela de espera (imagem + texto) exibida enquanto uma live está reconectando.
STANDBY_SLOT = "9"
STANDBY_MESSAGE_DEFAULT = "Voltaremos em breve..."

_LOG_PATH = _DATA_DIR / "daemon.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mpv-daemon] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(_LOG_PATH, encoding="utf-8")],
)
logger = logging.getLogger("mpv_daemon")


def _parse_end_reason(event) -> str:
    """Converte o evento end-file do MPV em 'eof', 'stop' ou 'error'.

    O motivo real vem em event.data.reason (inteiro do enum mpv_end_file_reason),
    não em event.reason diretamente — é preciso descer um nível.
    """
    try:
        raw = None
        if isinstance(event, dict):
            raw = event.get("reason")
            if raw is None:
                evt = event.get("event")
                raw = evt.get("reason") if isinstance(evt, dict) else getattr(evt, "reason", None)
        else:
            raw = getattr(event, "reason", None)
            if raw is None:
                data = getattr(event, "data", None)
                if data is not None:
                    raw = getattr(data, "reason", None)

        s = str(raw).lower() if raw is not None else ""
        try:
            r = int(raw)
        except (TypeError, ValueError):
            r = -1

        if r in (2, 3) or "stop" in s or "quit" in s:
            return "stop"
        if r == 4 or "error" in s:
            return "error"
        return "eof"
    except Exception:
        return "eof"


def _to_ytdl_url(url: str) -> str:
    """Força o hook ytdl (yt-dlp) do MPV a resolver a URL, mesmo fora da whitelist padrão."""
    return url if url.startswith("ytdl://") else f"ytdl://{url}"


def _ytdl_format_for_quality(quality: str) -> str:
    """Monta o ytdl-format para limitar a altura do vídeo. '' = deixa o yt-dlp escolher (melhor).

    'quality' é 'auto' ou uma altura em pixels (ex: '1080', '1440'), descoberta
    dinamicamente pelo main.py a partir das qualidades reais disponíveis no vídeo/live.
    """
    if quality == "auto":
        return ""
    try:
        height = int(quality)
    except (TypeError, ValueError):
        return ""
    if height <= 0:
        return ""
    return f"bestvideo[height<=?{height}]+bestaudio/best[height<=?{height}]"


def _get_secondary_monitor_rect() -> Optional[tuple]:
    """Retorna (left, top, right, bottom) do monitor secundário, ou None (Windows)."""
    try:
        import ctypes
        import ctypes.wintypes

        monitors = []

        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.wintypes.RECT),
            ctypes.c_double,
        )

        def _cb(_hmon, _hdc, lprc, _data):
            r = lprc.contents
            monitors.append((r.left, r.top, r.right, r.bottom))
            return True

        ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)

        for left, top, right, bottom in monitors:
            if left != 0 or top != 0:
                return (left, top, right, bottom)
        if len(monitors) >= 2:
            return monitors[1]
    except Exception as exc:
        logger.warning("Não foi possível detectar monitor secundário: %s", exc)
    return None


def _secondary_monitor_geometry() -> str:
    """Retorna a geometry MPV do monitor secundário no formato 'WxH+X+Y', ou '' se não houver."""
    rect = _get_secondary_monitor_rect()
    if not rect:
        return ""
    left, top, right, bottom = rect
    return f"{right - left}x{bottom - top}+{left}+{top}"


def _find_standby_image() -> Optional[Path]:
    """Retorna a primeira imagem encontrada em backend/../images/, ou None."""
    if not IMAGES_DIR.is_dir():
        return None
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        matches = sorted(IMAGES_DIR.glob(ext))
        if matches:
            return matches[0]
    return None


def _load_standby_font(size: int):
    from PIL import ImageFont
    for name in ("segoeuib.ttf", "segoeui.ttf", "arial.ttf", "verdana.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _contrasting_text_color(rgb) -> tuple:
    r, g, b = rgb[0], rgb[1], rgb[2]
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return (20, 30, 46, 255) if luminance > 140 else (255, 255, 255, 255)


def _render_standby(osd_w: int, osd_h: int, message: str) -> Optional[tuple]:
    """Renderiza a imagem de espera + texto centralizados, cobrindo toda a tela OSD.

    Retorna (bgra_bytes, width, height) ou None se Pillow não estiver disponível.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow não instalado — não é possível exibir a tela de espera")
        return None

    canvas = Image.new("RGBA", (osd_w, osd_h), (15, 17, 23, 255))

    img_path = _find_standby_image()
    if img_path is not None:
        try:
            logo = Image.open(str(img_path)).convert("RGBA")
            bg_color = logo.convert("RGB").getpixel((0, 0))
            canvas = Image.new("RGBA", (osd_w, osd_h), (*bg_color, 255))

            max_w = int(osd_w * 0.6)
            max_h = int(osd_h * 0.55)
            ratio = min(max_w / logo.width, max_h / logo.height, 1.0)
            new_w = max(1, int(logo.width * ratio))
            new_h = max(1, int(logo.height * ratio))
            logo = logo.resize((new_w, new_h), Image.LANCZOS)

            logo_x = (osd_w - new_w) // 2
            logo_y = int(osd_h * 0.46) - new_h // 2
            canvas.alpha_composite(logo, (logo_x, logo_y))
        except Exception as exc:
            logger.warning("Falha ao carregar imagem de espera %s: %s", img_path, exc)

    draw = ImageDraw.Draw(canvas)
    font_size = max(16, int(osd_h * 0.045))
    font = _load_standby_font(font_size)

    bbox = draw.textbbox((0, 0), message, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = (osd_w - text_w) // 2 - bbox[0]
    text_y = int(osd_h * 0.74) - bbox[1]

    sample = canvas.getpixel((osd_w // 2, min(osd_h - 1, int(osd_h * 0.74))))
    text_color = _contrasting_text_color(sample)
    draw.text((text_x, text_y), message, font=font, fill=text_color)

    rgba = canvas.tobytes()
    bgra = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        fa = a / 255.0
        bgra[i] = int(b * fa)
        bgra[i + 1] = int(g * fa)
        bgra[i + 2] = int(r * fa)
        bgra[i + 3] = a

    return bytes(bgra), osd_w, osd_h


def _move_window_to_secondary(window_title: str) -> None:
    """Reposiciona a janela do MPV no monitor secundário, acima da barra de tarefas.

    Usa SetWindowPos em vez de toggle de fullscreen para não invalidar o VO do MPV.
    """
    import ctypes

    rect = _get_secondary_monitor_rect()
    if not rect:
        return

    hwnd = None
    for _ in range(30):
        hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        logger.warning("Janela '%s' não encontrada para reposicionar", window_title)
        return

    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    # HWND_TOPMOST=-1, SWP_NOACTIVATE=0x0010, SWP_SHOWWINDOW=0x0040
    ctypes.windll.user32.SetWindowPos(
        hwnd, ctypes.c_int(-1), left, top, width, height, 0x0010 | 0x0040
    )
    logger.info("Janela '%s' posicionada no monitor secundário: %dx%d+%d+%d",
                window_title, width, height, left, top)


class MPVDaemon:
    def __init__(self):
        self._mpv = None
        self._mpv_dead = True
        self._clients: list[asyncio.StreamWriter] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._position = 0.0
        self._duration: Optional[float] = None
        self._title: Optional[str] = None
        self._volume = 100.0
        self._quality = "auto"
        self._is_live = False
        self._window_positioned = False  # move_to_secondary() só roda uma vez por sessão MPV

    def _build_raw_options(self) -> str:
        """Monta --ytdl-raw-options: cookies, runtime JS, e o cliente mweb para lives.

        Algumas lives só resolvem via o cliente "mweb" (mobile web); usá-lo sempre
        reduz a qualidade disponível em vídeos normais, então só entra quando
        self._is_live é True (decidido pelo main.py ao consultar a URL).
        """
        parts = [f"js-runtimes=node:{NODE_EXE_PATH}"]
        if COOKIES_PATH.is_file():
            parts.append(f"cookies={COOKIES_PATH}")
        if self._is_live:
            parts.append("extractor-args=youtube:player_client=mweb")
        return ",".join(parts)

    # Inicialização do MPV

    def _init_mpv(self):
        import mpv

        self._mpv_dead = False
        self._window_positioned = False

        geo = _secondary_monitor_geometry()
        has_secondary = bool(geo)

        mpv_kwargs = dict(
            ytdl=True,
            input_default_bindings=False,
            input_vo_keyboard=False,
            video_sync="display-resample",
            hr_seek="yes",
            keep_open=False,
            idle=True,
            force_window="immediate",   # janela permanece aberta (tela preta) ao parar
            title="YTPlayer",
            log_handler=self._mpv_log,
            loglevel="warn",
            volume_max=200,
        )

        if has_secondary:
            # Sem borda/barra de título, tela cheia e sempre no topo — cobre a barra de tarefas
            # panscan=1: corta o mínimo necessário das bordas para preencher a tela cheia sem
            # tarjas pretas quando a proporção do monitor não é exatamente 16:9.
            mpv_kwargs.update(border=False, fullscreen=True, ontop=True, geometry=geo, panscan=1.0)
            logger.info("Monitor secundário detectado — vídeo em tela cheia: %s", geo)
        else:
            mpv_kwargs.update(border=True, fullscreen=False, ontop=False, geometry="1024x576")
            logger.warning("Monitor secundário não detectado — janela normal no monitor principal")

        fmt = _ytdl_format_for_quality(self._quality)
        if fmt:
            mpv_kwargs["ytdl_format"] = fmt

        # ytdl_hook-ytdl_path: usa o yt-dlp próprio (empacotado junto), não o do sistema.
        # js-runtimes=node:<path>: idem para o Node.js — sem isso o yt-dlp só tenta "deno"
        # por padrão para resolver o desafio JS (nsig) do YouTube, dando "No video formats found".
        if not YTDLP_EXE_PATH.is_file():
            logger.warning("YTPlayer-ytdlp.exe não encontrado (%s) — usando yt-dlp do PATH do sistema, se houver", YTDLP_EXE_PATH)
        if not NODE_EXE_PATH.is_file():
            logger.warning("node.exe não encontrado (%s) — resolução de vídeos do YouTube pode falhar", NODE_EXE_PATH)
        if COOKIES_PATH.is_file():
            logger.info("Usando cookies.txt para autenticação no YouTube: %s", COOKIES_PATH)
        else:
            logger.warning("cookies.txt não encontrado (%s) — requisições ao YouTube sem autenticação", COOKIES_PATH)

        if YTDLP_EXE_PATH.is_file():
            mpv_kwargs["script_opts"] = f"ytdl_hook-ytdl_path={YTDLP_EXE_PATH}"
        mpv_kwargs["ytdl_raw_options"] = self._build_raw_options()

        self._mpv = mpv.MPV(**mpv_kwargs)
        self._mpv.volume = self._volume

        # Com force_window=immediate a janela abre antes de qualquer clipe;
        # posiciona no monitor correto logo ao inicializar.
        if has_secondary:
            threading.Thread(target=self._move_to_secondary, daemon=True).start()

        self._mpv.observe_property("time-pos", self._on_time_pos)
        self._mpv.observe_property("duration", self._on_duration)
        self._mpv.observe_property("media-title", self._on_title)

        @self._mpv.event_callback("file-loaded")
        def _file_loaded(event):
            self._remove_standby()  # conteúdo real carregou — some com a tela de espera
            if has_secondary and not self._window_positioned:
                threading.Thread(target=self._move_to_secondary, daemon=True).start()

        @self._mpv.event_callback("end-file")
        def _end_file(event):
            if self._mpv_dead:
                return
            reason = _parse_end_reason(event)
            logger.info("end-file reason=%s", reason)
            self._broadcast_sync({"event": "end-file", "reason": reason})

        @self._mpv.event_callback("shutdown")
        def _shutdown(event):
            if self._mpv_dead:
                return
            self._mpv_dead = True
            logger.info("MPV encerrado — reinit ocorrerá no próximo play")
            self._broadcast_sync({"event": "mpv_closed"})

        logger.info("MPV inicializado")

    def _mpv_log(self, level, component, message):
        logger.debug("[mpv/%s] %s", component, message.strip())

    def _move_to_secondary(self):
        _move_window_to_secondary("YTPlayer")
        self._window_positioned = True

    # Tela de espera (imagem + texto)

    def _osd_dims(self) -> tuple:
        try:
            w = int(self._mpv.osd_width or self._mpv.width or 1920)
            h = int(self._mpv.osd_height or self._mpv.height or 1080)
            return w, h
        except Exception:
            return 1920, 1080

    def _apply_standby(self, message: str):
        if not self._mpv or self._mpv_dead:
            return
        osd_w, osd_h = self._osd_dims()
        result = _render_standby(osd_w, osd_h, message)
        if result is None:
            return
        bgra_bytes, w, h = result
        tmp = Path(tempfile.gettempdir()) / "ytplayer_standby.bgra"
        try:
            tmp.write_bytes(bgra_bytes)
        except Exception as exc:
            logger.warning("Falha ao gravar tela de espera: %s", exc)
            return
        try:
            self._mpv.command(
                "overlay-add", STANDBY_SLOT, "0", "0", str(tmp), "0", "bgra", str(w), str(h), str(w * 4),
            )
            logger.info("Tela de espera exibida: %r", message)
        except Exception as exc:
            logger.warning("overlay-add (standby) falhou: %s", exc)

    def _remove_standby(self):
        if not self._mpv or self._mpv_dead:
            return
        try:
            self._mpv.command("overlay-remove", STANDBY_SLOT)
        except Exception:
            pass

    # Callbacks de propriedade

    def _on_time_pos(self, name, value):
        if value is not None:
            self._position = float(value)

    def _on_duration(self, name, value):
        self._duration = float(value) if value else None

    def _on_title(self, name, value):
        self._title = value

    # Estado / broadcast

    def _status_snapshot(self) -> dict:
        paused = False
        playing = False
        if self._mpv and not self._mpv_dead:
            try:
                paused = bool(self._mpv.pause)
            except Exception:
                pass
            try:
                playing = self._mpv.path is not None
            except Exception:
                pass
        return {
            "event": "status",
            "playing": playing,
            "paused": paused,
            "position": round(self._position, 2),
            "duration": self._duration,
            "title": self._title,
            "volume": self._volume,
            "quality": self._quality,
        }

    def _broadcast_sync(self, data: dict):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._broadcast(data), self._loop)

    async def _broadcast(self, data: dict):
        payload = (json.dumps(data) + "\n").encode()
        dead = []
        for w in self._clients:
            try:
                w.write(payload)
                await w.drain()
            except Exception:
                dead.append(w)
        for w in dead:
            if w in self._clients:
                self._clients.remove(w)

    # Servidor TCP

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        logger.info("Cliente conectado: %s", addr)
        self._clients.append(writer)

        try:
            writer.write((json.dumps(self._status_snapshot()) + "\n").encode())
            await writer.drain()
        except Exception:
            pass

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    try:
                        await self._handle_command(json.loads(line), writer)
                    except json.JSONDecodeError:
                        pass
        except Exception as exc:
            logger.debug("Erro no cliente %s: %s", addr, exc)
        finally:
            if writer in self._clients:
                self._clients.remove(writer)
            try:
                writer.close()
            except Exception:
                pass
            logger.info("Cliente desconectado: %s", addr)

    async def _handle_command(self, cmd: dict, writer: asyncio.StreamWriter):
        action = cmd.get("action")

        if action == "play":
            url = cmd.get("url", "")
            if not url:
                return
            if self._mpv_dead or self._mpv is None:
                logger.info("Reinicializando MPV para reprodução...")
                self._init_mpv()
            self._is_live = bool(cmd.get("is_live", False))
            try:
                self._mpv.ytdl_format = _ytdl_format_for_quality(self._quality)
                self._mpv.ytdl_raw_options = self._build_raw_options()
            except Exception as exc:
                logger.warning("ytdl options: %s", exc)
            self._duration = None
            self._title = None
            self._mpv.command("loadfile", _to_ytdl_url(url), "replace")
            self._mpv.pause = False
            logger.info("play: %s (qualidade=%s, is_live=%s)", url, self._quality, self._is_live)

        elif action == "pause":
            if self._mpv and not self._mpv_dead:
                self._mpv.pause = True

        elif action == "resume":
            if self._mpv and not self._mpv_dead:
                self._mpv.pause = False

        elif action == "stop":
            if self._mpv and not self._mpv_dead:
                self._mpv.command("stop")
            self._position = 0.0
            self._duration = None
            self._title = None

        elif action == "seek":
            if self._mpv and not self._mpv_dead:
                try:
                    self._mpv.seek(cmd.get("seconds", 0.0), cmd.get("mode", "absolute"))
                except Exception as exc:
                    logger.warning("seek: %s", exc)

        elif action == "set_volume":
            vol = max(0.0, min(200.0, float(cmd.get("volume", 100.0))))
            self._volume = vol
            if self._mpv and not self._mpv_dead:
                try:
                    self._mpv.volume = vol
                except Exception as exc:
                    logger.warning("set_volume: %s", exc)

        elif action == "set_quality":
            quality = str(cmd.get("quality", "auto")).strip()
            if quality != "auto":
                try:
                    if int(quality) <= 0:
                        quality = "auto"
                except ValueError:
                    quality = "auto"
            self._quality = quality
            logger.info("Qualidade definida: %s", quality)
            if self._mpv and not self._mpv_dead:
                try:
                    self._mpv.ytdl_format = _ytdl_format_for_quality(quality)
                    self._mpv.ytdl_raw_options = self._build_raw_options()
                except Exception as exc:
                    logger.warning("set_quality (ytdl options): %s", exc)
                try:
                    current_path = self._mpv.path
                except Exception:
                    current_path = None
                if current_path:
                    self._duration = None
                    self._title = None
                    self._mpv.command("loadfile", current_path, "replace")
                    self._mpv.pause = False
                    logger.info("Recarregando com nova qualidade: %s", current_path)

        elif action == "show_standby":
            message = str(cmd.get("message") or STANDBY_MESSAGE_DEFAULT)
            self._apply_standby(message)

        elif action == "hide_standby":
            self._remove_standby()

        elif action == "quit_daemon":
            logger.info("quit_daemon recebido — encerrando MPV e o processo do daemon")
            mpv_ref = self._mpv
            self._mpv_dead = True
            self._mpv = None

            def _terminate_and_exit():
                if mpv_ref is not None:
                    try:
                        mpv_ref.terminate()
                    except Exception as exc:
                        logger.warning("quit_daemon terminate: %s", exc)
                time.sleep(0.2)
                os._exit(0)

            threading.Thread(target=_terminate_and_exit, daemon=True).start()

        elif action == "get_state":
            try:
                writer.write((json.dumps(self._status_snapshot()) + "\n").encode())
                await writer.drain()
            except Exception:
                pass

    async def _status_task(self):
        while True:
            await asyncio.sleep(0.5)
            if self._clients:
                await self._broadcast(self._status_snapshot())

    async def serve(self):
        self._loop = asyncio.get_event_loop()
        self._init_mpv()
        server = await asyncio.start_server(self.handle_client, HOST, PORT)
        logger.info("MPV Daemon escutando em %s:%d — PID %d", HOST, PORT, os.getpid())
        asyncio.create_task(self._status_task())
        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    logger.info("Daemon iniciado — log em: %s", _LOG_PATH)
    d = MPVDaemon()
    try:
        asyncio.run(d.serve())
    except KeyboardInterrupt:
        logger.info("Daemon encerrado")

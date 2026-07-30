"""Player — cliente TCP para o MPV Daemon (processo separado).

Conecta-se a 127.0.0.1:6601; se o daemon não estiver rodando, inicia-o.
O daemon sobrevive a recargas da interface e a crashes do servidor FastAPI.
"""

import json
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 6601
_DAEMON_SCRIPT = Path(__file__).parent.parent / "mpv_daemon.py"

logger = logging.getLogger(__name__)


class Player:
    def __init__(
        self,
        on_status: Optional[Callable[[dict], None]] = None,
        on_end_file: Optional[Callable[[str], None]] = None,
    ):
        self._on_status = on_status
        self._on_end_file = on_end_file
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._connected = False
        self._buf = b""
        self._connect_or_start()

    # Conexão

    def _connect_or_start(self):
        if self._try_connect():
            logger.info("Conectado ao MPV Daemon existente em %s:%d", DAEMON_HOST, DAEMON_PORT)
            return
        logger.info("Daemon não encontrado — iniciando...")
        self._launch_daemon()
        for _ in range(30):
            time.sleep(0.3)
            if self._try_connect():
                logger.info("MPV Daemon iniciado com sucesso")
                return
        logger.error("Não foi possível conectar ao MPV Daemon após 9s")

    def _launch_daemon(self):
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            if getattr(sys, "frozen", False):
                # Empacotado: o daemon é um .exe próprio (YTPlayer-daemon.exe),
                # copiado para a mesma pasta de distribuição do YTPlayer.exe.
                daemon_exe = Path(sys.executable).parent / "YTPlayer-daemon.exe"
                cmd = [str(daemon_exe)]
            else:
                cmd = [sys.executable, str(_DAEMON_SCRIPT)]
            subprocess.Popen(cmd, creationflags=flags)
        except Exception as exc:
            logger.error("Falha ao iniciar daemon: %s", exc)

    def _try_connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect((DAEMON_HOST, DAEMON_PORT))
            s.settimeout(None)
            self._sock = s
            self._connected = True
            threading.Thread(target=self._read_loop, daemon=True).start()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    # Leitura de eventos

    def _read_loop(self):
        while self._connected:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    logger.warning("MPV Daemon encerrou a conexão")
                    self._connected = False
                    break
                self._buf += chunk
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            self._handle_event(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except Exception as exc:
                if self._connected:
                    logger.error("Erro de leitura: %s", exc)
                self._connected = False
                break

        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._buf = b""
        logger.info("Tentando reconectar ao MPV Daemon em 3s...")
        time.sleep(3)
        self._connect_or_start()

    def _handle_event(self, msg: dict):
        event = msg.get("event")
        if event == "status":
            if self._on_status:
                self._on_status(msg)
        elif event == "end-file":
            if self._on_end_file:
                self._on_end_file(msg.get("reason", "eof"))
        elif event == "mpv_closed":
            if self._on_status:
                self._on_status({
                    "event": "status", "playing": False, "paused": False,
                    "position": 0.0, "duration": None, "title": None, "volume": 100.0,
                })

    # Envio

    def _send(self, cmd: dict):
        if not self._connected or self._sock is None:
            logger.debug("Daemon não conectado — ignorando: %s", cmd.get("action"))
            return
        with self._send_lock:
            try:
                self._sock.sendall((json.dumps(cmd) + "\n").encode())
            except Exception as exc:
                logger.error("Erro ao enviar: %s", exc)
                self._connected = False

    # API pública

    def play(self, url: str, is_live: bool = False):
        self._send({"action": "play", "url": url, "is_live": is_live})
        logger.info("Reproduzindo: %s (is_live=%s)", url, is_live)

    def pause(self):
        self._send({"action": "pause"})

    def resume(self):
        self._send({"action": "resume"})

    def stop(self):
        self._send({"action": "stop"})

    def seek(self, seconds: float, mode: str = "absolute"):
        self._send({"action": "seek", "seconds": seconds, "mode": mode})

    def set_volume(self, volume: float):
        self._send({"action": "set_volume", "volume": volume})

    def set_quality(self, quality: str):
        self._send({"action": "set_quality", "quality": quality})

    def show_standby(self, message: str = "Voltaremos em breve..."):
        self._send({"action": "show_standby", "message": message})

    def hide_standby(self):
        self._send({"action": "hide_standby"})

    def quit_daemon(self):
        """Encerra o MPV e o processo do daemon TCP por completo (não sobrevive a este comando)."""
        self._send({"action": "quit_daemon"})
        logger.info("Solicitando encerramento total do MPV Daemon")

    def shutdown(self):
        """Fecha a conexão com o daemon (daemon continua rodando independentemente)."""
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        logger.info("Conexão com MPV Daemon encerrada (daemon continua rodando)")

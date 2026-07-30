const state = {
  duration: 0,
  position: 0,
  paused: true,
  playing: false,
  seeking: false,
  volumePercent: 100,
};

const els = {
  urlForm: document.getElementById('url-form'),
  urlInput: document.getElementById('url-input'),
  videoTitle: document.getElementById('video-title'),
  statusDot: document.getElementById('status-dot'),
  statusText: document.getElementById('status-text'),
  timeCurrent: document.getElementById('time-current'),
  timeDuration: document.getElementById('time-duration'),
  seekBar: document.getElementById('seek-bar'),
  playPauseBtn: document.getElementById('playpause-btn'),
  stopBtn: document.getElementById('stop-btn'),
  volumeSlider: document.getElementById('volume-slider'),
  volumeLabel: document.getElementById('volume-label'),
  qualitySelect: document.getElementById('quality-select'),
  connectionStatus: document.getElementById('connection-status'),
};

let ws = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.event === 'status') applyStatus(msg);
    else if (msg.event === 'reconnecting') applyReconnecting(msg);
    else if (msg.event === 'reconnect_failed') applyReconnectFailed(msg);
    else if (msg.event === 'end-file' && msg.reason === 'stop') clearConnectionStatus();
    else if (msg.event === 'available_qualities') applyAvailableQualities(msg);
  });

  ws.addEventListener('close', () => setTimeout(connect, 1500));
  ws.addEventListener('error', () => ws.close());
}

function send(action, payload = {}) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action, ...payload }));
  }
}

function fmtTime(sec) {
  if (!sec || sec < 0 || !isFinite(sec)) return '00:00';
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function applyReconnecting(msg) {
  let text;
  if (msg.no_internet) {
    text = `Sem conexão com a internet — verifique sua rede (tentativa ${msg.attempt}/${msg.max_attempts}) em ${msg.delay}s…`;
  } else if (msg.never_played) {
    text = `Link inválido ou vídeo indisponível — tentando novamente (tentativa ${msg.attempt}/${msg.max_attempts}) em ${msg.delay}s…`;
  } else {
    text = `Live caiu — tentando reconectar (tentativa ${msg.attempt}/${msg.max_attempts}) em ${msg.delay}s…`;
  }
  els.connectionStatus.textContent = text;
  els.connectionStatus.classList.remove('status-error');
  els.connectionStatus.classList.add('status-warning', 'visible');
}

function applyReconnectFailed(msg) {
  let text;
  if (msg.no_internet) {
    text = 'Sem conexão com a internet — verifique sua rede.';
  } else if (msg.never_played) {
    text = 'Não foi possível reproduzir este link — verifique se a URL está correta.';
  } else {
    text = 'Não foi possível reconectar à live após várias tentativas.';
  }
  els.connectionStatus.textContent = text;
  els.connectionStatus.classList.remove('status-warning');
  els.connectionStatus.classList.add('status-error', 'visible');
}

function clearConnectionStatus() {
  els.connectionStatus.textContent = '';
  els.connectionStatus.classList.remove('visible', 'status-warning', 'status-error');
}

function applyAvailableQualities(msg) {
  const heights = msg.heights || [];
  const select = els.qualitySelect;
  const previousValue = select.value;

  select.innerHTML = '';
  const autoOpt = document.createElement('option');
  autoOpt.value = 'auto';
  autoOpt.textContent = 'Automática';
  select.appendChild(autoOpt);

  heights.forEach((h) => {
    const opt = document.createElement('option');
    opt.value = String(h);
    opt.textContent = `${h}p`;
    select.appendChild(opt);
  });

  const stillExists = Array.from(select.options).some((o) => o.value === previousValue);
  select.value = stillExists ? previousValue : 'auto';
}

function applyStatus(msg) {
  state.duration = msg.duration || 0;
  state.paused = !!msg.paused;
  state.playing = !!msg.playing;
  if (typeof msg.volume === 'number') state.volumePercent = msg.volume;
  if (msg.playing) clearConnectionStatus();
  if (msg.quality && document.activeElement !== els.qualitySelect) {
    els.qualitySelect.value = msg.quality;
  }

  if (!state.seeking) {
    state.position = msg.position || 0;
    els.seekBar.value = state.duration > 0
      ? Math.round((state.position / state.duration) * 1000)
      : 0;
    els.timeCurrent.textContent = fmtTime(state.position);
  }
  els.timeDuration.textContent = state.duration ? fmtTime(state.duration) : '--:--';

  els.videoTitle.textContent = msg.title || (state.playing ? 'Carregando…' : 'Nenhum vídeo carregado');
  els.statusText.textContent = state.playing ? (state.paused ? 'Pausado' : 'Reproduzindo') : '—';
  els.statusDot.classList.remove('is-playing', 'is-paused');
  if (state.playing) {
    els.statusDot.classList.add(state.paused ? 'is-paused' : 'is-playing');
  }
  els.playPauseBtn.textContent = (state.paused || !state.playing) ? '▶' : '⏸';

  const db = state.volumePercent > 0 ? 20 * Math.log10(state.volumePercent / 100) : -20;
  const dbClamped = Math.round(Math.max(-20, Math.min(6, db)));
  els.volumeSlider.value = dbClamped;
  els.volumeLabel.textContent = `${dbClamped} dB`;
}

els.urlForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const url = els.urlInput.value.trim();
  if (!url) return;
  send('play', { url });
  els.urlInput.value = '';
});

els.playPauseBtn.addEventListener('click', () => send('toggle_pause'));
els.stopBtn.addEventListener('click', () => send('stop'));

els.seekBar.addEventListener('input', () => {
  state.seeking = true;
  const frac = els.seekBar.value / 1000;
  els.timeCurrent.textContent = fmtTime(frac * state.duration);
});

els.seekBar.addEventListener('change', () => {
  const frac = els.seekBar.value / 1000;
  send('seek', { seconds: frac * state.duration, mode: 'absolute' });
  state.seeking = false;
});

els.volumeSlider.addEventListener('input', () => {
  const db = Number(els.volumeSlider.value);
  els.volumeLabel.textContent = `${db} dB`;
  const percent = 100 * Math.pow(10, db / 20);
  send('set_volume', { volume: percent });
});

els.qualitySelect.addEventListener('change', () => {
  send('set_quality', { quality: els.qualitySelect.value });
});

connect();

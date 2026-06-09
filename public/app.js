const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const video = $('#video');
const urlInput = $('#url-input');
const queueList = $('#queue-list');
const historyList = $('#history-list');
const favoritesList = $('#favorites-list');
const downloadsList = $('#downloads-list');
const libraryList = $('#library-list');
const playlistsContainer = $('#playlists-container');
const torrentList = $('#torrent-list');
const cloudStatus = $('#cloud-status');
const cloudStatusText = $('#cloud-status-text');
const cloudProgressBar = $('#cloud-progress-bar');
let torrentPollTimer = null;
let cloudPollActive = false;
let appStatus = null;
const qualitySelect = $('#quality-select');
const subtitleSelect = $('#subtitle-select');
const speedSelect = $('#speed-select');
const overlay = $('#player-overlay');
const metaTitle = $('#meta-title');
const metaDetails = $('#meta-details');
const metaThumb = $('#meta-thumb');
const nowPlaying = $('#now-playing');
const progressHitarea = $('#progress-hitarea');
const progressBar = $('#progress-bar');
const progressPlayed = $('#progress-played');
const progressBuffered = $('#progress-buffered');
const progressThumb = $('#progress-thumb');
const seekPreview = $('#seek-preview');
const seekPreviewTime = $('#seek-preview-time');
const timeCurrent = $('#time-current');
const timeTotal = $('#time-total');

let hls = null;
let dashPlayer = null;
let currentMedia = null;
let fitMode = localStorage.getItem('nexus-fit-mode') || 'contain';
const FOCUS_KEY = 'nexus-video-focus';
let videoFocusMode = localStorage.getItem(FOCUS_KEY) === '1';
const LIBRARY_KEY = 'nexus-library-v1';
let prefetchController = null;
let prefetchBlobUrl = null;
let prefetchFileUrl = null;
let prefetchState = { percent: 0, active: false, url: null };
const MAX_BLOB_CACHE = 2 * 1024 * 1024 * 1024;
const HLS_MAX_BUFFER_SEC = 72000;

const FIT_MODES = ['contain', 'cover', 'fill', 'none'];
const FIT_LABELS = { contain: 'Fit', cover: 'Crop', fill: 'Stretch', none: 'Original' };
let queue = [];
let queueIndex = -1;
let hideTimer = null;
let progressSaveTimer = null;
let ws = null;
let isSeeking = false;
let loopVideo = false;
let shuffleQueue = false;
let repeatQueue = false;
let abPointA = null;
let abPointB = null;
let abStep = 0;
let audioCtx = null;
let gainNode = null;
let audioBoostReady = false;

function attachApiError(detail, status) {
  let message = `Error ${status}`;
  const err = new Error(message);
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    message = detail.error || detail.message || message;
    err.message = message;
    err.code = detail.code || null;
    err.hint = detail.hint || null;
    err.retriable = !!detail.retriable;
    err.site = detail.site || null;
    return err;
  }
  if (typeof detail === 'string') err.message = detail;
  else if (detail) err.message = String(detail);
  return err;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw attachApiError(data.detail, res.status);
  return data;
}

function toast(msg, ms = 2800) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function fmtTime(s) {
  if (!s || !isFinite(s)) return '0:00';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function fmtSize(b) {
  if (!b) return '';
  if (b > 1e9) return (b / 1e9).toFixed(1) + ' GB';
  if (b > 1e6) return (b / 1e6).toFixed(1) + ' MB';
  return (b / 1e3).toFixed(0) + ' KB';
}

function fmtSpeed(bps) {
  if (!bps) return '0 B/s';
  if (bps > 1e6) return (bps / 1e6).toFixed(1) + ' MB/s';
  if (bps > 1e3) return (bps / 1e3).toFixed(0) + ' KB/s';
  return bps + ' B/s';
}

function isMagnet(url) {
  return /^magnet:\?/i.test((url || '').trim());
}

function getDuration() {
  if (isFinite(video.duration) && video.duration > 0) return video.duration;
  if (currentMedia?.duration) return currentMedia.duration;
  return 0;
}

function destroyHls() {
  if (hls) { hls.destroy(); hls = null; }
}

function destroyDash() {
  if (dashPlayer) { dashPlayer.reset(); dashPlayer = null; }
}

function setFitMode(mode) {
  if (!FIT_MODES.includes(mode)) mode = 'contain';
  fitMode = mode;
  localStorage.setItem('nexus-fit-mode', mode);
  video.className = '';
  video.classList.add('fit-' + mode);
  $('#fit-select').value = mode;
  $$('.fit-btn').forEach((btn) => btn.classList.toggle('active', btn.dataset.fit === mode));
  api('/api/settings', { method: 'PUT', body: JSON.stringify({ key: 'fit_mode', value: mode }) }).catch(() => {});
}

function cycleFitMode() {
  const idx = FIT_MODES.indexOf(fitMode);
  setFitMode(FIT_MODES[(idx + 1) % FIT_MODES.length]);
  toast('Display: ' + FIT_LABELS[fitMode]);
}

function setupAudioBoost() {
  if (audioBoostReady) return;
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = audioCtx.createMediaElementSource(video);
    gainNode = audioCtx.createGain();
    source.connect(gainNode);
    gainNode.connect(audioCtx.destination);
    gainNode.gain.value = 1;
    audioBoostReady = true;
  } catch (_) {}
}

async function tryPlay() {
  if (audioCtx?.state === 'suspended') {
    await audioCtx.resume().catch(() => {});
  }
  try {
    await video.play();
    return true;
  } catch (err) {
    toast('Playback blocked — tap the video or play button', 5000);
    setPlayPauseIcon(false);
    return false;
  }
}

function setVolume(val) {
  const v = val / 100;
  if (v > 1) setupAudioBoost();
  if (gainNode) {
    gainNode.gain.value = Math.min(v, 2);
    video.volume = Math.min(v, 1);
  } else {
    video.volume = Math.min(v, 1);
  }
  video.muted = v === 0;
  setMuteIcon(video.muted);
}

function getLocalLibrary() {
  try {
    return JSON.parse(localStorage.getItem(LIBRARY_KEY) || '{"history":[],"favorites":[]}');
  } catch (_) {
    return { history: [], favorites: [] };
  }
}

function saveLocalLibrary(lib) {
  localStorage.setItem(LIBRARY_KEY, JSON.stringify(lib));
}

function mergeByUrl(items, key = 'url') {
  const seen = new Map();
  for (const item of items) {
    const k = item[key];
    if (!k) continue;
    if (!seen.has(k)) seen.set(k, item);
  }
  return [...seen.values()];
}

function trackLocalHistory(entry) {
  const lib = getLocalLibrary();
  const url = entry.url;
  if (!url) return;
  lib.history = lib.history.filter((h) => h.url !== url);
  lib.history.unshift({
    ...entry,
    played_at: entry.played_at || new Date().toISOString(),
  });
  lib.history = lib.history.slice(0, 300);
  saveLocalLibrary(lib);
}

function trackLocalFavorite(entry) {
  const lib = getLocalLibrary();
  if (!entry.url || lib.favorites.some((f) => f.url === entry.url)) return;
  lib.favorites.unshift({ ...entry, added_at: new Date().toISOString() });
  lib.favorites = lib.favorites.slice(0, 200);
  saveLocalLibrary(lib);
}

function updateLocalHistoryPosition(url, position) {
  const lib = getLocalLibrary();
  const item = lib.history.find((h) => h.url === url);
  if (item) {
    item.position = position;
    item.played_at = new Date().toISOString();
    saveLocalLibrary(lib);
  }
}

async function syncLibrary() {
  const local = getLocalLibrary();
  try {
    const merged = await api('/api/library/sync', {
      method: 'POST',
      body: JSON.stringify(local),
    });
    saveLocalLibrary({
      history: merged.history || local.history,
      favorites: merged.favorites || local.favorites,
    });
  } catch (_) {}
}

function getBufferedPercent() {
  const dur = getDuration();
  if (!dur || !video.buffered.length) return 0;
  let maxEnd = 0;
  for (let i = 0; i < video.buffered.length; i++) {
    maxEnd = Math.max(maxEnd, video.buffered.end(i));
  }
  return (maxEnd / dur) * 100;
}

function setPrefetchPercent(pct) {
  prefetchState.percent = Math.min(100, Math.max(0, pct));
  updateBufferBar();
}

function updateBufferBar() {
  const bufPct = Math.max(getBufferedPercent(), prefetchState.percent);
  progressBuffered.style.width = bufPct + '%';
}

function resetPrefetch() {
  if (prefetchController) prefetchController.abort();
  if (prefetchBlobUrl) {
    URL.revokeObjectURL(prefetchBlobUrl);
    prefetchBlobUrl = null;
  }
  if (prefetchFileUrl) {
    URL.revokeObjectURL(prefetchFileUrl);
    prefetchFileUrl = null;
  }
  prefetchState = { percent: 0, active: false, url: null };
  progressBuffered.style.width = '0%';
}

function swapToLocalPlayback(localUrl) {
  const t = video.currentTime;
  const playing = !video.paused;
  video.src = localUrl;
  video.addEventListener('loadedmetadata', () => {
    if (t > 0) video.currentTime = t;
    if (playing) tryPlay();
    setPrefetchPercent(100);
  }, { once: true });
}

function hasOpfs() {
  return typeof navigator.storage?.getDirectory === 'function';
}

async function prefetchToOpfs(url, total, signal) {
  const root = await navigator.storage.getDirectory();
  const handle = await root.getFileHandle('nexus-prefetch.bin', { create: true });
  const writable = await handle.createWritable();
  const res = await fetch(url, { signal });
  if (!res.ok || !res.body) return;
  const reader = res.body.getReader();
  let received = 0;
  prefetchState.active = true;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    await writable.write(value);
    received += value.length;
    if (total) setPrefetchPercent((received / total) * 100);
    else if (received) setPrefetchPercent(Math.min(99, received / (50 * 1024 * 1024)));
  }
  await writable.close();
  const file = await handle.getFile();
  if (prefetchFileUrl) URL.revokeObjectURL(prefetchFileUrl);
  prefetchFileUrl = URL.createObjectURL(file);
  prefetchState.active = false;
  swapToLocalPlayback(prefetchFileUrl);
}

async function prefetchToBlob(url, total, signal) {
  const res = await fetch(url, { signal });
  if (!res.ok || !res.body) return;
  const reader = res.body.getReader();
  const chunks = [];
  let received = 0;
  prefetchState.active = true;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    if (total) setPrefetchPercent((received / total) * 100);
  }
  if (prefetchBlobUrl) URL.revokeObjectURL(prefetchBlobUrl);
  const blob = new Blob(chunks, { type: res.headers.get('content-type') || 'video/mp4' });
  prefetchBlobUrl = URL.createObjectURL(blob);
  prefetchState.active = false;
  swapToLocalPlayback(prefetchBlobUrl);
}

async function prefetchProgressOnly(url, total, signal) {
  const res = await fetch(url, { signal });
  if (!res.ok || !res.body) return;
  const reader = res.body.getReader();
  let received = 0;
  prefetchState.active = true;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.length;
    if (total) setPrefetchPercent((received / total) * 100);
  }
  prefetchState.active = false;
  setPrefetchPercent(100);
}

async function startFullPrefetch(url, signal) {
  let total = 0;
  try {
    const head = await fetch(url, { method: 'HEAD', signal });
    total = +(head.headers.get('content-length') || 0);
  } catch (_) {}

  prefetchState.url = url;
  try {
    if (await hasOpfs()) {
      await prefetchToOpfs(url, total, signal);
      return;
    }
    if (!total || total <= MAX_BLOB_CACHE) {
      await prefetchToBlob(url, total, signal);
      return;
    }
    await prefetchProgressOnly(url, total, signal);
  } catch (err) {
    if (err.name !== 'AbortError') prefetchState.active = false;
  }
}

async function loadProgressive(url) {
  resetPrefetch();
  prefetchController = new AbortController();
  const signal = prefetchController.signal;
  video.preload = 'auto';
  video.src = url;
  video.addEventListener('canplay', () => tryPlay(), { once: true });
  startFullPrefetch(url, signal);
}

function onVideoElementError() {
  const code = video.error?.code;
  const labels = {
    1: 'Playback aborted',
    2: 'Network error while loading stream',
    3: 'Stream decode failed',
    4: 'Format not supported in browser',
  };
  const detail = labels[code] || 'Video playback failed';
  handlePlaybackError(detail, {
    message: detail,
    hint: 'Try another quality, MPV, or a direct .mp4/.m3u8 link.',
    retriable: true,
  });
}

function loadSource(url, type = 'progressive') {
  destroyHls();
  destroyDash();
  resetPrefetch();
  video.removeAttribute('src');
  video.load();
  video.onerror = onVideoElementError;

  if (type === 'dash' && typeof dashjs !== 'undefined') {
    dashPlayer = dashjs.MediaPlayer().create();
    dashPlayer.updateSettings({
      streaming: {
        buffer: {
          bufferTimeAtTopQuality: HLS_MAX_BUFFER_SEC,
          bufferTimeAtTopQualityLongForm: HLS_MAX_BUFFER_SEC,
          bufferTimeAtTopQualityMobile: 3600,
          longFormContentDurationThreshold: 600,
          stableBufferTime: 60,
        },
        gaps: { jumpGaps: true },
      },
    });
    dashPlayer.initialize(video, url, true);
    video.addEventListener('canplay', () => tryPlay(), { once: true });
    dashPlayer.on(dashjs.MediaPlayer.events.BUFFER_LEVEL_UPDATED, () => updateBufferBar());
    dashPlayer.on(dashjs.MediaPlayer.events.ERROR, (e) => {
      if (!e?.error) return;
      handlePlaybackError('DASH stream error', {
        message: e.error.message || 'DASH playback failed',
        hint: 'Try another quality or open in MPV.',
        retriable: true,
      });
    });
  } else if (type === 'hls' && typeof Hls !== 'undefined' && Hls.isSupported()) {
    hls = new Hls({
      enableWorker: true,
      maxBufferLength: 600,
      maxMaxBufferLength: HLS_MAX_BUFFER_SEC,
      maxBufferSize: 16 * 1024 * 1024 * 1024,
      backBufferLength: 300,
      progressive: true,
      startFragPrefetch: true,
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => tryPlay());
    hls.on(Hls.Events.BUFFER_APPENDED, () => updateBufferBar());
    hls.on(Hls.Events.FRAG_BUFFERED, () => updateBufferBar());
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (!data.fatal) return;
      const hint = data.type === Hls.ErrorTypes.NETWORK_ERROR
        ? 'Network error — stream may have expired. Replay the link or try MPV.'
        : 'Try another quality or open in MPV.';
      handlePlaybackError('HLS stream error', {
        message: data.details || data.type || 'HLS playback failed',
        hint,
        retriable: true,
      });
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        try { hls.startLoad(); } catch (_) {}
      }
    });
  } else if (type === 'hls' && video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    video.addEventListener('loadedmetadata', () => tryPlay(), { once: true });
  } else {
    loadProgressive(url);
    video.addEventListener('loadedmetadata', () => {
      if (video.duration > 0 && video.duration < 1 && video.seekable.length) {
        toast('Stream looks invalid — try MPV or replay the magnet', 6000);
      }
    }, { once: true });
  }
}

function isPikpakMedia(info) {
  if (!info) return false;
  return info.site === 'pikpak'
    || info.resolved_with === 'pikpak-cloud'
    || info.pikpak_task_id
    || (info.source_url && isMagnet(info.source_url) && appStatus?.pikpak?.configured);
}

function formatPikpakPhase(phase) {
  const labels = {
    running: 'Downloading to cloud',
    pending: 'Queued',
    complete: 'Ready in cloud',
    error: 'Failed',
  };
  return labels[phase] || phase || 'Cloud';
}

function findMatchingPikpakTask(tasks, info) {
  if (!tasks?.length || !info) return null;
  if (info.pikpak_task_id) {
    const byTask = tasks.find((t) => t.task_id === info.pikpak_task_id || t.id === info.pikpak_task_id);
    if (byTask) return byTask;
  }
  if (info.pikpak_file_id) {
    const byFile = tasks.find((t) => t.file_id === info.pikpak_file_id);
    if (byFile) return byFile;
  }
  if (info.source_url) {
    const src = info.source_url.trim();
    const bySource = tasks.find((t) => t.source && t.source.trim() === src);
    if (bySource) return bySource;
  }
  if (info.title) {
    const title = info.title.toLowerCase();
    const byName = tasks.find((t) => (t.name || '').toLowerCase().includes(title.split(' — ')[0].toLowerCase()));
    if (byName) return byName;
  }
  return null;
}

function renderCloudStatus(task) {
  if (!task) {
    cloudStatus.classList.add('hidden');
    return;
  }
  cloudStatus.classList.remove('hidden', 'complete', 'error', 'running');
  cloudStatus.classList.add(task.phase || 'running');
  const parts = [
    'PikPak cloud',
    formatPikpakPhase(task.phase),
    `${task.progress || 0}%`,
  ];
  if (task.file_size) parts.push(fmtSize(task.file_size));
  if (task.download_rate) parts.push(`↓ ${fmtSpeed(task.download_rate)}`);
  if (task.phase === 'complete') parts.push('streaming from cloud');
  if (task.error && task.phase !== 'complete') parts.push(task.error);
  cloudStatusText.textContent = parts.filter(Boolean).join(' · ');
  cloudProgressBar.style.width = `${Math.min(100, task.progress || 0)}%`;
}

async function updateCloudStatus() {
  if (!appStatus?.pikpak?.configured || !isPikpakMedia(currentMedia)) {
    cloudStatus.classList.add('hidden');
    return;
  }
  try {
    const tasks = await api('/api/pikpak/tasks');
    renderCloudStatus(findMatchingPikpakTask(tasks, currentMedia) || tasks[0]);
  } catch (_) {
    cloudStatus.classList.add('hidden');
  }
}

function setMeta(info) {
  metaTitle.textContent = info.title || '—';
  const parts = [];
  if (info.site) parts.push(info.site);
  if (info.uploader) parts.push(info.uploader);
  if (info.duration) parts.push(fmtTime(info.duration));
  if (info.view_count) parts.push(info.view_count.toLocaleString() + ' views');
  metaDetails.textContent = parts.join(' · ') || '';
  metaThumb.style.backgroundImage = info.thumbnail ? `url(${info.thumbnail})` : '';
  nowPlaying.textContent = info.title || 'Playing';
  if (isPikpakMedia(info)) updateCloudStatus();
  else cloudStatus.classList.add('hidden');
  refreshFavoriteState();
}

function setPlayPauseIcon(playing) {
  const btn = $('#toggle-btn');
  if (!btn) return;
  btn.querySelector('.ico-play')?.toggleAttribute('hidden', playing);
  btn.querySelector('.ico-pause')?.toggleAttribute('hidden', !playing);
  btn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
}

function setMuteIcon(muted) {
  const btn = $('#mute-btn');
  if (!btn) return;
  btn.querySelector('.ico-vol')?.toggleAttribute('hidden', muted);
  btn.querySelector('.ico-muted')?.toggleAttribute('hidden', !muted);
}

function setFullscreenIcon(isFs) {
  const btn = $('#fs-btn');
  if (!btn) return;
  btn.querySelector('.ico-enter-fs')?.toggleAttribute('hidden', isFs);
  btn.querySelector('.ico-exit-fs')?.toggleAttribute('hidden', !isFs);
}

function isFullscreen() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement);
}

function syncMenuSelects() {
  const pairs = [
    [qualitySelect, $('#menu-quality')],
    [subtitleSelect, $('#menu-subtitle')],
    [speedSelect, $('#menu-speed')],
    [$('#fit-select'), $('#menu-fit')],
  ];
  pairs.forEach(([from, to]) => {
    if (!from || !to) return;
    to.innerHTML = from.innerHTML;
    to.value = from.value;
  });
}

function syncMenuActionStates() {
  ['loop-btn', 'shuffle-btn', 'repeat-btn', 'ab-btn', 'fav-btn'].forEach((id) => {
    const src = $(`#${id}`);
    const tile = document.querySelector(`[data-trigger="${id}"]`);
    if (src && tile) tile.classList.toggle('active', src.classList.contains('active'));
  });
}

function initPlayerMenu() {
  const speeds = ['0.25', '0.5', '0.75', '1', '1.25', '1.5', '1.75', '2', '3', '4'];
  if (speedSelect.options.length < 5) {
    speedSelect.innerHTML = speeds.map((s) => `<option value="${s}">${s}×</option>`).join('');
    speedSelect.value = '1';
  }

  const bindMenuSelect = (menuId, target) => {
    const menu = $(menuId);
    if (!menu || !target) return;
    menu.addEventListener('change', () => {
      target.value = menu.value;
      target.dispatchEvent(new Event('change'));
    });
  };

  syncMenuSelects();
  bindMenuSelect('#menu-quality', qualitySelect);
  bindMenuSelect('#menu-subtitle', subtitleSelect);
  bindMenuSelect('#menu-speed', speedSelect);
  bindMenuSelect('#menu-fit', $('#fit-select'));

  $('#player-menu-btn')?.addEventListener('click', () => {
    syncMenuSelects();
    syncMenuActionStates();
    $('#player-menu-dialog')?.showModal();
  });

  $$('[data-close-dialog]').forEach((btn) => {
    btn.addEventListener('click', () => btn.closest('dialog')?.close());
  });

  $$('[data-trigger]').forEach((tile) => {
    tile.addEventListener('click', () => {
      $(`#${tile.dataset.trigger}`)?.click();
      syncMenuActionStates();
    });
  });
}

function populateFormats(formats, selected) {
  qualitySelect.innerHTML = '<option value="">Auto quality</option>';
  formats.forEach((f) => {
    const opt = document.createElement('option');
    opt.value = f.format_id;
    opt.textContent = [f.quality, f.resolution, f.ext, fmtSize(f.filesize)].filter(Boolean).join(' · ') || f.format_id;
    if (f.format_id === selected) opt.selected = true;
    qualitySelect.appendChild(opt);
  });
  syncMenuSelects();
}

function populateSubtitles(subs) {
  subtitleSelect.innerHTML = '<option value="">No subtitles</option>';
  subs.forEach((s, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = s.name || s.lang;
    subtitleSelect.appendChild(opt);
  });
  syncMenuSelects();
}

/* ── Seek / progress bar ── */
function pctFromX(clientX) {
  const rect = progressBar.getBoundingClientRect();
  return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
}

function seekToPct(pct, preview = false) {
  const dur = getDuration();
  if (!dur) return;
  const t = pct * dur;
  if (preview) {
    seekPreviewTime.textContent = fmtTime(t);
    seekPreview.style.left = (pct * 100) + '%';
    seekPreview.classList.add('visible');
    progressPlayed.style.width = (pct * 100) + '%';
    progressThumb.style.left = (pct * 100) + '%';
    return;
  }
  video.currentTime = t;
  updateProgress();
  wsSend({ cmd: 'seek', time: t });
}

function startSeek(clientX) {
  isSeeking = true;
  progressHitarea.classList.add('seeking');
  seekToPct(pctFromX(clientX), true);
}

function endSeek(clientX) {
  if (!isSeeking) return;
  seekToPct(pctFromX(clientX), false);
  isSeeking = false;
  progressHitarea.classList.remove('seeking');
  seekPreview.classList.remove('visible');
}

function updateProgress() {
  const dur = getDuration();
  const pct = dur ? (video.currentTime / dur) * 100 : 0;
  if (!isSeeking) {
    progressPlayed.style.width = pct + '%';
    progressThumb.style.left = pct + '%';
  }
  timeCurrent.textContent = fmtTime(video.currentTime);
  timeTotal.textContent = fmtTime(dur);

  updateBufferBar();

  if (abPointA !== null && abPointB !== null && dur) {
    const aPct = (abPointA / dur) * 100;
    const bPct = (abPointB / dur) * 100;
    $('#ab-marker-a').style.left = aPct + '%';
    $('#ab-marker-b').style.left = bPct + '%';
    $('#ab-range').style.left = aPct + '%';
    $('#ab-range').style.width = (bPct - aPct) + '%';
    if (video.currentTime >= abPointB) video.currentTime = abPointA;
  }
}

function updateAbMarkers() {
  const dur = getDuration();
  const show = abPointA !== null;
  $('#ab-marker-a').classList.toggle('hidden', !show);
  $('#ab-marker-b').classList.toggle('hidden', abPointB === null);
  $('#ab-range').classList.toggle('hidden', abPointA === null || abPointB === null);
  if (show && dur) updateProgress();
}

function recordPlayback(info) {
  const source = info.source_url || info.url;
  if (!source) return;
  trackLocalHistory({
    url: source,
    title: info.title,
    thumbnail: info.thumbnail,
    duration: info.duration || 0,
    site: info.site,
    format_id: info.best_format_id,
    position: 0,
  });
  api('/api/library/sync', {
    method: 'POST',
    body: JSON.stringify(getLocalLibrary()),
  }).catch(() => {});
}

async function playResolved(info) {
  currentMedia = info;
  overlay.classList.add('hidden');
  setMeta(info);
  populateFormats(info.formats || [], info.best_format_id);
  populateSubtitles(info.subtitles || []);
  loadSource(info.play_url, info.stream_type);
  recordPlayback(info);
  toast('Playing: ' + (info.title || 'media'));
  wsSend({ cmd: 'nowplaying', title: info.title, url: info.source_url });
  if (isPikpakMedia(info)) updateCloudStatus();
  cloudPollActive = isPikpakMedia(info);
}

async function playTorrent(tid, fileIndex = null) {
  toast('Starting torrent stream...', 5000);
  const body = fileIndex != null ? { file_index: fileIndex } : {};
  const info = await api(`/api/torrent/${tid}/play`, { method: 'POST', body: JSON.stringify(body) });
  currentMedia = info;
  overlay.classList.add('hidden');
  setMeta(info);
  populateFormats(info.formats || [], info.best_format_id);
  populateSubtitles(info.subtitles || []);
  loadSource(info.play_url, info.stream_type);
  toast('Playing torrent: ' + (info.title || 'media'));
}

async function addMagnet(magnet, autoplay = true) {
  const fd = new FormData();
  fd.append('magnet', magnet.trim());
  toast('Adding torrent...', 5000);
  const res = await fetch('/api/torrent/add', { method: 'POST', body: fd }).then((r) => {
    if (!r.ok) return r.json().then((d) => Promise.reject(new Error(d.detail || 'Add failed')));
    return r.json();
  });
  await refreshTorrents();
  toast('Torrent added: ' + (res.name || 'magnet'));
  if (autoplay) {
    if ((res.type === 'pikpak' || res.pikpak) && res.play_url) {
      await playResolved(res);
    } else if (res.type === 'pikpak' || res.pikpak) {
      await playUrl(magnet);
    } else {
      const videoFile = (res.files || []).find((f) => f.is_video);
      if (videoFile) await playTorrent(res.id, videoFile.index);
    }
  }
  return res;
}

function renderLocalTorrentCard(t) {
  const card = document.createElement('li');
  card.className = 'torrent-card';
  card.innerHTML = `
    <div class="torrent-card-header">
      <div>
        <div class="torrent-name">${esc(t.name || 'Torrent')}</div>
        <div class="torrent-meta">Local · ${esc(t.state || '')} · ${t.peers || 0} peers · ↓ ${fmtSpeed(t.download_rate)} · ↑ ${fmtSpeed(t.upload_rate)}</div>
      </div>
      <div class="torrent-actions">
        <button type="button" data-act="play" title="Play best video">▶</button>
        <button type="button" data-act="${t.paused ? 'resume' : 'pause'}">${t.paused ? '▶' : '⏸'}</button>
        <button type="button" data-act="remove" title="Remove">✕</button>
      </div>
    </div>
    <div class="torrent-progress"><div class="torrent-progress-bar" style="width:${t.progress || 0}%"></div></div>
    <div class="torrent-files" data-files="${t.id}">Loading files...</div>
  `;

  card.querySelectorAll('.torrent-actions button').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const act = btn.dataset.act;
      if (act === 'play') {
        try { await playTorrent(t.id); } catch (err) { toast(err.message, 5000); }
      } else if (act === 'pause') {
        await api(`/api/torrent/${t.id}/pause`, { method: 'POST' });
        refreshTorrents();
      } else if (act === 'resume') {
        await api(`/api/torrent/${t.id}/resume`, { method: 'POST' });
        refreshTorrents();
      } else if (act === 'remove') {
        await api(`/api/torrent/${t.id}?delete_files=false`, { method: 'DELETE' });
        refreshTorrents();
      }
    });
  });

  torrentList.appendChild(card);
  loadTorrentFiles(t.id, card.querySelector(`[data-files="${t.id}"]`));
}

function renderPikpakTaskCard(t) {
  const card = document.createElement('li');
  card.className = 'torrent-card cloud';
  const phase = formatPikpakPhase(t.phase || t.state);
  const size = t.file_size ? fmtSize(t.file_size) : '';
  const speed = t.download_rate ? `↓ ${fmtSpeed(t.download_rate)}` : '';
  const canPlay = t.phase === 'complete' && t.source;
  card.innerHTML = `
    <div class="torrent-card-header">
      <div>
        <div class="torrent-name">${esc(t.name || 'PikPak download')}</div>
        <div class="torrent-meta">PikPak cloud · ${esc(phase)} · ${t.progress || 0}%${size ? ` · ${size}` : ''}${speed ? ` · ${speed}` : ''}</div>
      </div>
      <div class="torrent-actions">
        ${canPlay ? '<button type="button" data-act="play" title="Play from cloud">▶</button>' : ''}
      </div>
    </div>
    <div class="torrent-progress"><div class="torrent-progress-bar" style="width:${t.progress || 0}%"></div></div>
    ${t.error ? `<div class="torrent-meta" style="margin-top:0.35rem;color:var(--danger,#e55)">${esc(t.error)}</div>` : ''}
  `;

  const playBtn = card.querySelector('[data-act="play"]');
  if (playBtn) {
    playBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      try { await playUrl(t.source); } catch (err) { toast(err.message, 5000); }
    });
  }

  torrentList.appendChild(card);
}

async function refreshTorrents() {
  const localAvailable = appStatus?.torrent_available;
  const pikpakAvailable = appStatus?.pikpak?.configured;
  torrentList.innerHTML = '';

  if (!localAvailable && !pikpakAvailable) {
    torrentList.innerHTML = '<li class="item"><div class="item-sub">Configure PikPak or enable local libtorrent to manage torrents</div></li>';
    return;
  }

  const [localItems, cloudItems] = await Promise.all([
    localAvailable ? api('/api/torrent').catch(() => []) : Promise.resolve([]),
    pikpakAvailable ? api('/api/pikpak/tasks').catch(() => []) : Promise.resolve([]),
  ]);

  if (!localItems.length && !cloudItems.length) {
    torrentList.innerHTML = '<li class="item"><div class="item-sub">No active torrents — paste a magnet or drop a .torrent file</div></li>';
    return;
  }

  cloudItems.forEach(renderPikpakTaskCard);
  localItems.forEach(renderLocalTorrentCard);
}

async function loadTorrentFiles(tid, container) {
  try {
    const files = await api(`/api/torrent/${tid}/files`);
    container.innerHTML = '';
    files.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'torrent-file' + (f.is_video ? ' playable' : '');
      row.innerHTML = `<span>${esc(f.name)} (${f.progress || 0}%)</span><span class="torrent-file-size">${fmtSize(f.size)}</span>`;
      if (f.is_video) {
        row.addEventListener('click', async () => {
          try { await playTorrent(tid, f.index); } catch (e) { toast(e.message, 5000); }
        });
      }
      container.appendChild(row);
    });
  } catch (_) {
    container.textContent = 'Files unavailable';
  }
}

async function initPikpak() {
  try {
    const st = appStatus?.pikpak || await api('/api/pikpak/status');
    const el = $('#pikpak-status');
    if (st.username) $('#pikpak-user').value = st.username;
    $('#pikpak-enabled').checked = st.enabled !== false;
    if (st.configured) {
      el.textContent = `Logged in as ${st.username} — torrents use PikPak cloud`;
      el.classList.add('ok');
    } else if (st.available) {
      el.textContent = 'Enter PikPak email & password to stream via cloud';
    } else {
      el.textContent = 'PikPak module not installed on server';
    }
  } catch (_) {}
}

function shouldPollCloudStatus() {
  return appStatus?.pikpak?.configured && (
    cloudPollActive
    || isPikpakMedia(currentMedia)
  );
}

function startTorrentPolling() {
  clearInterval(torrentPollTimer);
  torrentPollTimer = setInterval(() => {
    if ($('#panel-torrents').classList.contains('active')) refreshTorrents();
    else if (shouldPollCloudStatus()) updateCloudStatus();
  }, 3000);
}

function isBlockedSiteError(msg) {
  return /403|forbidden|blocked this server|cloud servers|sign in|confirm your age|bot|captcha|unable to extract/i.test(msg || '');
}

function shouldTryLocalBridge(err) {
  if (!err) return false;
  if (err.code === 'site_blocked' || err.code === 'youtube_blocked' || err.code === 'youtube_auth_required' || err.code === 'kvs_failed') {
    return true;
  }
  return err.retriable !== false && isBlockedSiteError(err.message);
}

function formatPlayError(err) {
  const parts = [err?.message || 'Playback failed'];
  if (err?.hint) parts.push(err.hint);
  return parts.join(' — ');
}

function handlePlaybackError(context, err) {
  const msg = formatPlayError(err);
  toast(`${context}: ${msg}`, 9000);
  if (nowPlaying) nowPlaying.textContent = 'Playback error';
}

async function resolveViaLocalBridge(url) {
  const bridge = `http://127.0.0.1:${MPV_BRIDGE_PORT}/resolve?url=${encodeURIComponent(url)}`;
  const res = await fetch(bridge, { signal: AbortSignal.timeout(30000) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) throw new Error(data.error || 'Local resolve failed');
  return data;
}

function applyPlayback(info, resumePos = 0) {
  currentMedia = info;
  overlay.classList.add('hidden');
  setMeta(info);
  populateFormats(info.formats || [], info.best_format_id);
  populateSubtitles(info.subtitles || []);
  loadSource(info.play_url, info.stream_type);

  const seekTo = () => {
    if (resumePos > 0 && getDuration()) video.currentTime = resumePos;
  };
  video.addEventListener('loadedmetadata', seekTo, { once: true });
  video.addEventListener('durationchange', seekTo, { once: true });

  recordPlayback(info);
  toast('Playing: ' + (info.title || 'media'));
  wsSend({ cmd: 'nowplaying', title: info.title, url: info.source_url });
  if (isPikpakMedia(info)) updateCloudStatus();
}

async function playViaLocalBridge(url, formatId = null, resumePos = 0) {
  toast('Cloud blocked — resolving on your PC...', 8000);
  const resolved = await resolveViaLocalBridge(url);
  const info = await api('/api/play/local', {
    method: 'POST',
    body: JSON.stringify({
      source_url: url,
      stream_url: resolved.stream_url,
      title: resolved.title,
      thumbnail: resolved.thumbnail,
      duration: resolved.duration,
      site: resolved.site,
      headers: resolved.headers,
      stream_type: resolved.stream_type || 'progressive',
      content_type: resolved.content_type,
      resolved_with: resolved.resolved_with || 'local-bridge',
    }),
  });
  applyPlayback(info, resumePos);
  toast('Playing via local bridge: ' + (info.title || 'media'), 5000);
}

async function playUrl(url, formatId = null, resumePos = 0) {
  if (!url) return;
  const pikpakMagnet = isMagnet(url) && appStatus?.pikpak?.configured;
  if (pikpakMagnet) {
    toast('Sending to PikPak cloud...', 8000);
    cloudPollActive = true;
    updateCloudStatus();
  } else if (isMagnet(url)) {
    toast('Starting torrent — fetching metadata...', 8000);
  } else {
    toast('Resolving stream...');
  }
  urlInput.value = url;

  try {
    const info = await api('/api/play', {
      method: 'POST',
      body: JSON.stringify({ url, format_id: formatId }),
    });

    if (info.type === 'playlist') {
      toast(`Queued ${info.queued} items from playlist`);
      await refreshQueue();
      if (queue.length) {
        queueIndex = 0;
        await playQueueItem(0);
      }
      return;
    }

    applyPlayback(info, resumePos);
  } catch (e) {
    if (shouldTryLocalBridge(e)) {
      try {
        await playViaLocalBridge(url, formatId, resumePos);
        return;
      } catch (localErr) {
        toast('Local resolve failed — run start-mpv-bridge.vbs on your PC, then try again', 10000);
        toast(formatPlayError(localErr), 8000);
        return;
      }
    }
    handlePlaybackError('Could not play link', e);
  } finally {
    cloudPollActive = isPikpakMedia(currentMedia);
  }
}

async function playQueueItem(index) {
  if (index < 0 || index >= queue.length) return;
  queueIndex = index;
  renderQueue();
  await playUrl(queue[index].url, queue[index].format_id);
}

function renderList(el, items, onClick, extra = '') {
  el.innerHTML = '';
  items.forEach((item, i) => {
    const li = document.createElement('li');
    li.className = 'item' + (extra === 'queue' && i === queueIndex ? ' active' : '');
    li.innerHTML = `
      <div class="item-title">${esc(item.title || item.url || item.filename || 'Untitled')}</div>
      <div class="item-sub">${esc(item.site || item.url || item.size || '')}</div>
      ${item.progress !== undefined ? `<div class="item-progress"><div class="item-progress-bar" style="width:${item.progress}%"></div></div>` : ''}
    `;
    li.addEventListener('click', () => onClick(item, i));
    el.appendChild(li);
  });
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderQueue() {
  renderList(queueList, queue, (item, i) => playQueueItem(i), 'queue');
}

async function refreshQueue() {
  queue = await api('/api/queue');
  renderQueue();
}

async function refreshHistory() {
  const local = getLocalLibrary().history || [];
  const server = await api('/api/history').catch(() => []);
  const merged = mergeByUrl([...local, ...server]).slice(0, 300);
  saveLocalLibrary({ ...getLocalLibrary(), history: merged });
  renderList(historyList, merged, (item) => playUrl(item.url, item.format_id, item.position || 0));
}

async function refreshFavorites() {
  const local = getLocalLibrary().favorites || [];
  const server = await api('/api/favorites').catch(() => []);
  const merged = mergeByUrl([...local, ...server]);
  saveLocalLibrary({ ...getLocalLibrary(), favorites: merged });
  renderList(favoritesList, merged, (item) => playUrl(item.url));
}

async function refreshDownloads() {
  renderList(downloadsList, await api('/api/downloads'), (item) => {
    if (item.filepath) playLocal('/api/local/' + item.filepath.split(/[/\\]/).pop(), item.title);
  });
}

async function refreshLibrary() {
  const items = await api('/api/library');
  renderList(libraryList, items, (item) => playLocal(item.play_url, item.title));
}

async function refreshPlaylists() {
  const playlists = await api('/api/playlists');
  playlistsContainer.innerHTML = '';
  playlists.forEach((pl) => {
    const block = document.createElement('div');
    block.className = 'playlist-block';
    block.innerHTML = `<div class="playlist-header">${esc(pl.name)} (${pl.items.length})</div>`;
    const ul = document.createElement('ul');
    ul.className = 'item-list';
    pl.items.forEach((item) => {
      const li = document.createElement('li');
      li.className = 'item';
      li.innerHTML = `<div class="item-title">${esc(item.title || item.url)}</div>`;
      li.addEventListener('click', () => playUrl(item.url));
      ul.appendChild(li);
    });
    block.appendChild(ul);
    playlistsContainer.appendChild(block);
  });
}

function playLocal(url, title) {
  destroyHls();
  destroyDash();
  currentMedia = { title, play_url: url, stream_type: 'progressive', source_url: url };
  overlay.classList.add('hidden');
  setMeta({ title, site: 'local' });
  video.src = url;
  video.addEventListener('canplay', () => tryPlay(), { once: true });
}

function scheduleProgressSave() {
  if (!currentMedia?.source_url) return;
  clearTimeout(progressSaveTimer);
  progressSaveTimer = setTimeout(() => {
    api('/api/progress', {
      method: 'POST',
      body: JSON.stringify({
        url: currentMedia.source_url,
        position: video.currentTime,
        title: currentMedia.title,
        thumbnail: currentMedia.thumbnail,
        duration: getDuration(),
        site: currentMedia.site,
        format_id: currentMedia.best_format_id,
      }),
    }).catch(() => {});
    updateLocalHistoryPosition(currentMedia.source_url, video.currentTime);
  }, 3000);
}

function wsSend(data) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data));
}

function handleRemoteCmd(msg) {
  switch (msg.cmd) {
    case 'play': video.play(); break;
    case 'pause': video.pause(); break;
    case 'toggle': video.paused ? video.play() : video.pause(); break;
    case 'seek': if (msg.time != null) video.currentTime = msg.time; break;
    case 'volume': setVolume(msg.value); $('#volume-slider').value = msg.value; break;
    case 'load': if (msg.url) playUrl(msg.url); break;
  }
}

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    $('#status-dot').classList.add('online');
    $('#status-text').textContent = 'Connected · Remote ready';
  };
  ws.onclose = () => {
    $('#status-dot').classList.remove('online');
    $('#status-text').textContent = 'Reconnecting...';
    setTimeout(connectWs, 3000);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event) {
      if (['download_progress', 'download_complete', 'download_failed'].includes(msg.event)) {
        refreshDownloads();
        if (msg.event === 'download_complete') toast('Download complete');
      }
      return;
    }
    if (msg.cmd) handleRemoteCmd(msg);
  };
}

async function initStatus() {
  appStatus = await api('/api/status');
  const st = appStatus;
  const remote = `http://${st.lan_ip}:${st.port}`;
  $('#remote-url').textContent = remote + ' (tap to copy)';
  $('#remote-url').onclick = () => { navigator.clipboard.writeText(remote); toast('Remote URL copied'); };
  const mpvBtn = $('#mpv-btn');
  mpvBtn.style.opacity = '1';
  mpvBtn.title = st.mpv_server
    ? 'Open in MPV (server or local)'
    : 'Open in local MPV on your PC';
  if (!st.torrent_available && !st.pikpak?.configured) {
    $('#torrent-add-btn').style.opacity = '0.4';
  } else {
    $('#torrent-add-btn').style.opacity = '1';
  }
  await initPikpak();
  initMpvSettings();
  await syncLibrary();
  startTorrentPolling();
  refreshHistory();
  refreshFavorites();
}

async function initSettings() {
  try {
    const s = await api('/api/settings');
    if (s.fit_mode && FIT_MODES.includes(s.fit_mode)) setFitMode(s.fit_mode);
    if (s.volume) { $('#volume-slider').value = parseFloat(s.volume) * 100; setVolume(parseFloat(s.volume) * 100); }
    if (s.playback_rate) { speedSelect.value = s.playback_rate; video.playbackRate = parseFloat(s.playback_rate); }
  } catch (_) {}
}

function takeScreenshot() {
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  const a = document.createElement('a');
  a.href = canvas.toDataURL('image/png');
  a.download = (currentMedia?.title || 'screenshot').replace(/[^\w.-]/g, '_') + '.png';
  a.click();
  toast('Screenshot saved');
}

function toggleAbLoop() {
  if (abStep === 0) {
    abPointA = video.currentTime;
    abPointB = null;
    abStep = 1;
    toast('A-B Loop: point A set at ' + fmtTime(abPointA));
  } else if (abStep === 1) {
    abPointB = video.currentTime;
    if (abPointB <= abPointA) { abPointA = abPointB; abPointB = null; abStep = 1; toast('B must be after A'); return; }
    abStep = 2;
    $('#ab-btn').classList.add('active');
    toast('A-B Loop active');
  } else {
    abPointA = abPointB = null;
    abStep = 0;
    $('#ab-btn').classList.remove('active');
    toast('A-B Loop cleared');
  }
  updateAbMarkers();
}

function playNext() {
  if (!queue.length) return;
  if (shuffleQueue) {
    playQueueItem(Math.floor(Math.random() * queue.length));
    return;
  }
  if (queueIndex < queue.length - 1) playQueueItem(queueIndex + 1);
  else if (repeatQueue) playQueueItem(0);
}

/* ── Progress bar events ── */
progressHitarea.addEventListener('mousedown', (e) => {
  e.preventDefault();
  startSeek(e.clientX);
});

document.addEventListener('mousemove', (e) => {
  if (isSeeking) seekToPct(pctFromX(e.clientX), true);
});

document.addEventListener('mouseup', (e) => {
  if (isSeeking) endSeek(e.clientX);
});

progressHitarea.addEventListener('click', (e) => {
  if (!isSeeking) seekToPct(pctFromX(e.clientX), false);
});

progressHitarea.addEventListener('mousemove', (e) => {
  if (!isSeeking && getDuration()) {
    const pct = pctFromX(e.clientX);
    seekPreviewTime.textContent = fmtTime(pct * getDuration());
    seekPreview.style.left = (pct * 100) + '%';
    seekPreview.classList.add('visible');
  }
});

progressHitarea.addEventListener('mouseleave', () => {
  if (!isSeeking) seekPreview.classList.remove('visible');
});

progressHitarea.addEventListener('touchstart', (e) => {
  e.preventDefault();
  startSeek(e.touches[0].clientX);
}, { passive: false });

progressHitarea.addEventListener('touchmove', (e) => {
  e.preventDefault();
  seekToPct(pctFromX(e.touches[0].clientX), true);
}, { passive: false });

progressHitarea.addEventListener('touchend', (e) => {
  const touch = e.changedTouches[0];
  if (touch) endSeek(touch.clientX);
}, { passive: false });

/* ── Controls ── */
$('#play-btn').addEventListener('click', () => playUrl(urlInput.value.trim()));
$('#focus-btn')?.addEventListener('click', toggleVideoFocus);
$('#focus-exit-btn')?.addEventListener('click', toggleVideoFocus);
$('#torrent-add-btn').addEventListener('click', () => {
  const v = urlInput.value.trim();
  if (isMagnet(v)) addMagnet(v);
  else $('#torrent-dialog').showModal();
});
$('#torrent-paste-btn').addEventListener('click', () => $('#torrent-dialog').showModal());
$('#torrent-dialog').addEventListener('close', async () => {
  if ($('#torrent-dialog').returnValue !== 'ok') return;
  const magnet = $('#magnet-text').value.trim();
  if (!isMagnet(magnet)) { toast('Invalid magnet link'); return; }
  try { await addMagnet(magnet); } catch (e) { toast(e.message, 5000); }
});
$('#torrent-file-input').addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  toast('Adding torrent file...', 5000);
  try {
    const res = await fetch('/api/torrent/add', { method: 'POST', body: fd }).then((r) => {
      if (!r.ok) return r.json().then((d) => Promise.reject(new Error(d.detail || 'Failed')));
      return r.json();
    });
    await refreshTorrents();
    const videoFile = (res.files || []).find((f) => f.is_video);
    if (videoFile) await playTorrent(res.id, videoFile.index);
    else toast('Torrent added — pick a file to play');
  } catch (err) { toast(err.message, 5000); }
  e.target.value = '';
});
$('#refresh-torrents-btn').addEventListener('click', refreshTorrents);
$('#pikpak-save-btn').addEventListener('click', async () => {
  const username = $('#pikpak-user').value.trim();
  const password = $('#pikpak-pass').value;
  const enabled = $('#pikpak-enabled').checked;
  if (!username || !password) { toast('Enter PikPak email and password'); return; }
  try {
    await api('/api/pikpak/configure', { method: 'POST', body: JSON.stringify({ username, password, enabled }) });
    appStatus = await api('/api/status');
    await initPikpak();
    toast('PikPak connected — magnets will use your account');
    $('#pikpak-pass').value = '';
  } catch (e) { toast(e.message, 6000); }
});
$('#pikpak-enabled').addEventListener('change', async () => {
  try {
    await api('/api/pikpak/enable', { method: 'POST', body: JSON.stringify({ enabled: $('#pikpak-enabled').checked }) });
    appStatus = await api('/api/status');
    await initPikpak();
  } catch (e) { toast(e.message); }
});
$('#resolve-btn').addEventListener('click', async () => {
  const url = urlInput.value.trim();
  if (!url) return;
  try {
    const info = await api('/api/resolve', { method: 'POST', body: JSON.stringify({ url }) });
    const list = $('#formats-list');
    list.innerHTML = '';
    (info.formats || []).forEach((f) => {
      const row = document.createElement('div');
      row.className = 'format-row';
      row.innerHTML = `<span>${esc(f.quality || f.format_id)} · ${esc(f.resolution)} · ${f.ext}</span><span>${fmtSize(f.filesize)}</span>`;
      row.addEventListener('click', () => { $('#formats-dialog').close(); playUrl(url, f.format_id); });
      list.appendChild(row);
    });
    $('#formats-dialog').showModal();
  } catch (e) { toast(formatPlayError(e), 6000); }
});

const DEFAULT_MPV_PATH = 'C:\\mpv\\mpv\\mpv.exe';
const MPV_BRIDGE_PORT = '9340';

function getMpvPath() {
  return localStorage.getItem('nexus-mpv-path') || DEFAULT_MPV_PATH;
}

function setMpvPath(path) {
  localStorage.setItem('nexus-mpv-path', path.trim() || DEFAULT_MPV_PATH);
}

function initMpvSettings() {
  const input = $('#mpv-path');
  if (!input) return;
  input.value = getMpvPath();
  refreshMpvBridgeStatus();
}

async function refreshMpvBridgeStatus() {
  const el = $('#mpv-bridge-status');
  if (!el) return;
  const onLocalhost = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/i.test(window.location.origin);
  if (!onLocalhost) {
    el.textContent = 'Run start-mpv-bridge.vbs on your PC — needed for MPV and blocked sites';
    el.classList.remove('ok');
    return;
  }
  try {
    const res = await fetch(`http://127.0.0.1:${MPV_BRIDGE_PORT}/health`, { signal: AbortSignal.timeout(1200) });
    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      el.textContent = data.resolve
        ? 'Bridge running — MPV + local resolve for blocked sites'
        : 'MPV bridge running — MPV button launches directly';
      el.classList.add('ok');
      return;
    }
  } catch (_) {}
  el.textContent = 'Bridge not running — double-click start-mpv-bridge.vbs';
  el.classList.remove('ok');
}

function toAbsoluteUrl(path) {
  if (!path) return '';
  if (/^https?:\/\//i.test(path)) return path;
  return new URL(path, window.location.origin).href;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    return false;
  }
}

function buildMpvBridgeGetUrl(absUrl, title, referer = '') {
  const q = new URLSearchParams({
    url: absUrl,
    mpv: getMpvPath(),
    title: title || 'Nexus Player',
  });
  if (referer) q.set('referer', referer);
  return `http://127.0.0.1:${MPV_BRIDGE_PORT}/launch?${q}`;
}

function launchMpvViaLaunchPage(absUrl, title) {
  const page = new URL('/mpv-launch.html', window.location.origin);
  page.searchParams.set('url', absUrl);
  page.searchParams.set('mpv', getMpvPath());
  page.searchParams.set('title', title || 'Nexus Player');
  page.searchParams.set('port', MPV_BRIDGE_PORT);
  const w = window.open(page.href, 'nexusMpvLaunch', 'width=440,height=180');
  return !!w;
}

function tryOpenUrl(url, name) {
  const w = window.open(url, name || '_blank');
  if (w) return true;
  const a = document.createElement('a');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  return true;
}

function showMpvLaunchDialog(absUrl, title) {
  const bridgeUrl = buildMpvBridgeGetUrl(absUrl, title);
  const useForm = bridgeUrl.length > 5500 || absUrl.length > 1800;
  const link = $('#mpv-dialog-link');
  const formBtn = $('#mpv-dialog-form-btn');
  const status = $('#mpv-dialog-status');
  const form = $('#mpv-dialog-form');

  if (link) {
    if (useForm) {
      link.hidden = true;
      if (formBtn) formBtn.hidden = false;
      if (form) {
        form.action = `http://127.0.0.1:${MPV_BRIDGE_PORT}/launch-form`;
        $('#mpv-form-url').value = absUrl;
        $('#mpv-form-mpv').value = getMpvPath();
        $('#mpv-form-title').value = title || 'Nexus Player';
      }
    } else {
      link.hidden = false;
      link.href = bridgeUrl;
      if (formBtn) formBtn.hidden = true;
    }
  }

  if (status) {
    status.textContent = useForm
      ? `${title || 'Video'} — long URL: click Launch (long URL) after starting the bridge.`
      : `${title || 'Video'} — click Launch MPV (run start-mpv-bridge.vbs if nothing happens).`;
  }

  const dialog = $('#mpv-dialog');
  if (dialog) dialog.dataset.streamUrl = absUrl;
  dialog?.showModal();
}

async function launchResolvedInMpv(absUrl, title, referer = '') {
  if ($('#mpv-path')?.value) setMpvPath($('#mpv-path').value);
  const bridgeUrl = buildMpvBridgeGetUrl(absUrl, title, referer);
  const useForm = bridgeUrl.length > 5500 || absUrl.length > 1800;

  if (!useForm) {
    tryOpenUrl(bridgeUrl, 'nexusMpvBridge');
  } else {
    const page = new URL('/mpv-launch.html', window.location.origin);
    page.searchParams.set('url', absUrl);
    page.searchParams.set('mpv', getMpvPath());
    page.searchParams.set('title', title || 'Nexus Player');
    page.searchParams.set('port', MPV_BRIDGE_PORT);
    if (referer) page.searchParams.set('referer', referer);
    window.open(page.href, 'nexusMpvLaunch', 'width=440,height=180');
  }

  showMpvLaunchDialog(absUrl, title);
  await copyText(absUrl);
  toast('MPV launcher ready — click Launch MPV in the dialog if the player did not open.', 8000);
}

async function openInMpv() {
  let absUrl = '';
  let title = currentMedia?.title;
  let referer = currentMedia?.headers?.Referer || currentMedia?.source_url || '';

  if (currentMedia?.play_url) {
    absUrl = toAbsoluteUrl(currentMedia.play_url);
  } else {
    const source = urlInput.value.trim() || currentMedia?.source_url;
    if (!source) {
      toast('Load a video first, then click MPV');
      return;
    }
    try {
      const q = appStatus?.mpv_server ? '?server=true' : '';
      const res = await api(`/api/mpv${q}`, {
        method: 'POST',
        body: JSON.stringify({
          url: source,
          format_id: qualitySelect.value || null,
        }),
      });
      if (res.mode === 'server') {
        toast('Launched in MPV on this machine');
        return;
      }
      absUrl = res.play_url;
      title = res.title || title;
    } catch (e) {
      if (!isBlockedSiteError(e.message)) throw e;
      toast('Cloud blocked — resolving on your PC for MPV...', 8000);
      const resolved = await resolveViaLocalBridge(source);
      absUrl = resolved.stream_url;
      title = resolved.title || title;
      referer = resolved.headers?.Referer || source;
    }
  }

  if (!absUrl) {
    toast('No stream URL available for MPV');
    return;
  }

  await launchResolvedInMpv(absUrl, title, referer);
}

async function addToFavorites() {
  if (!currentMedia?.source_url) {
    toast('Play something first');
    return;
  }
  try {
    await api('/api/favorites', {
      method: 'POST',
      body: JSON.stringify({
        url: currentMedia.source_url,
        title: currentMedia.title,
        thumbnail: currentMedia.thumbnail,
        site: currentMedia.site,
      }),
    });
    trackLocalFavorite({
      url: currentMedia.source_url,
      title: currentMedia.title,
      thumbnail: currentMedia.thumbnail,
      site: currentMedia.site,
    });
    await syncLibrary();
    updateFavoriteButtons(true);
    toast('Added to favorites ★');
    refreshFavorites();
  } catch (e) {
    if (/409|already/i.test(e.message)) {
      updateFavoriteButtons(true);
      toast('Already in favorites');
    } else {
      toast(e.message, 5000);
    }
  }
}

function updateFavoriteButtons(isFav) {
  const on = !!isFav;
  $('#fav-btn')?.classList.toggle('active', on);
  $('#meta-fav-btn')?.classList.toggle('active', on);
  if ($('#meta-fav-btn')) {
    $('#meta-fav-btn').textContent = on ? 'Favorited' : 'Favorite';
    $('#meta-fav-btn').classList.toggle('active', on);
  }
  syncMenuActionStates();
}

async function refreshFavoriteState() {
  if (!currentMedia?.source_url) {
    updateFavoriteButtons(false);
    return;
  }
  const lib = getLocalLibrary().favorites || [];
  const server = await api('/api/favorites').catch(() => []);
  const isFav = [...lib, ...server].some((f) => f.url === currentMedia.source_url);
  updateFavoriteButtons(isFav);
}

async function showAddToPlaylistDialog() {
  if (!currentMedia?.source_url) {
    toast('Play something first');
    return;
  }
  const list = $('#playlist-pick-list');
  list.innerHTML = '<li>Loading…</li>';
  $('#playlist-dialog').showModal();
  try {
    const playlists = await api('/api/playlists');
    list.innerHTML = '';
    if (!playlists.length) {
      list.innerHTML = '<li class="item-sub">No playlists — create one below</li>';
      return;
    }
    playlists.forEach((pl) => {
      const li = document.createElement('li');
      li.textContent = `${pl.name} (${pl.items.length})`;
      li.addEventListener('click', async () => {
        try {
          await api(`/api/playlists/${pl.id}/items`, {
            method: 'POST',
            body: JSON.stringify({
              url: currentMedia.source_url,
              title: currentMedia.title,
            }),
          });
          $('#playlist-dialog').close();
          toast(`Added to "${pl.name}"`);
          refreshPlaylists();
        } catch (e) {
          toast(e.message, 5000);
        }
      });
      list.appendChild(li);
    });
  } catch (e) {
    list.innerHTML = `<li>${esc(e.message)}</li>`;
  }
}

$('#mpv-btn').addEventListener('click', async () => {
  try {
    await openInMpv();
  } catch (e) {
    toast(e.message, 6000);
  }
});

$('#mpv-save-path-btn')?.addEventListener('click', () => {
  setMpvPath($('#mpv-path').value);
  toast('MPV path saved: ' + getMpvPath());
});

$('#mpv-test-btn')?.addEventListener('click', () => {
  setMpvPath($('#mpv-path').value);
  const testUrl = 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4';
  tryOpenUrl(buildMpvBridgeGetUrl(testUrl, 'MPV test'), 'nexusMpvTest');
  showMpvLaunchDialog(testUrl, 'MPV test');
  toast('MPV test — run start-mpv-bridge.vbs if nothing opens', 8000);
});

$('#mpv-dialog-close')?.addEventListener('click', () => $('#mpv-dialog')?.close());
$('#mpv-dialog-link')?.addEventListener('click', () => {
  setTimeout(() => $('#mpv-dialog')?.close(), 600);
});
$('#mpv-dialog-form-btn')?.addEventListener('click', () => {
  $('#mpv-dialog-form')?.requestSubmit();
  toast('Sent to MPV bridge…');
});
$('#mpv-dialog-copy')?.addEventListener('click', async () => {
  const url = $('#mpv-dialog')?.dataset.streamUrl;
  if (url) {
    await copyText(url);
    toast('Stream URL copied');
  }
});

$('#new-playlist-from-dialog')?.addEventListener('click', async () => {
  const name = prompt('Playlist name');
  if (!name?.trim()) return;
  try {
    const pl = await api('/api/playlists', { method: 'POST', body: JSON.stringify({ name: name.trim() }) });
    await api(`/api/playlists/${pl.id}/items`, {
      method: 'POST',
      body: JSON.stringify({ url: currentMedia?.source_url, title: currentMedia?.title }),
    });
    $('#playlist-dialog').close();
    toast(`Created "${name}" and added video`);
    refreshPlaylists();
  } catch (e) {
    toast(e.message, 5000);
  }
});

$('#download-btn').addEventListener('click', async () => {
  const url = currentMedia?.source_url || urlInput.value.trim();
  if (!url) return;
  try {
    await api('/api/download', { method: 'POST', body: JSON.stringify({ url, format_id: qualitySelect.value || null }) });
    toast('Download started');
    refreshDownloads();
  } catch (e) { toast(e.message, 4000); }
});

$('#fav-btn').addEventListener('click', () => addToFavorites());
$('#playlist-btn')?.addEventListener('click', () => showAddToPlaylistDialog());
$('#meta-fav-btn')?.addEventListener('click', () => addToFavorites());
$('#meta-playlist-btn')?.addEventListener('click', () => showAddToPlaylistDialog());
$('#meta-mpv-btn')?.addEventListener('click', () => openInMpv().catch((e) => toast(e.message, 6000)));

$('#toggle-btn').addEventListener('click', async () => {
  if (video.paused) {
    const ok = await tryPlay();
    if (ok) wsSend({ cmd: 'play' });
  } else {
    video.pause();
    wsSend({ cmd: 'pause' });
  }
});

$('#prev-btn').addEventListener('click', () => { if (queueIndex > 0) playQueueItem(queueIndex - 1); });
$('#next-btn').addEventListener('click', playNext);

$('#loop-btn').addEventListener('click', () => {
  loopVideo = !loopVideo;
  video.loop = loopVideo;
  $('#loop-btn').classList.toggle('active', loopVideo);
  toast(loopVideo ? 'Loop on' : 'Loop off');
});

$('#shuffle-btn').addEventListener('click', () => {
  shuffleQueue = !shuffleQueue;
  $('#shuffle-btn').classList.toggle('active', shuffleQueue);
  syncMenuActionStates();
  toast(shuffleQueue ? 'Shuffle on' : 'Shuffle off');
});

$('#repeat-btn').addEventListener('click', () => {
  repeatQueue = !repeatQueue;
  $('#repeat-btn').classList.toggle('active', repeatQueue);
  syncMenuActionStates();
  toast(repeatQueue ? 'Repeat queue on' : 'Repeat queue off');
});

$('#ab-btn').addEventListener('click', toggleAbLoop);
$('#screenshot-btn').addEventListener('click', takeScreenshot);
$('#refresh-library-btn')?.addEventListener('click', refreshLibrary);

video.addEventListener('play', () => setPlayPauseIcon(true));
video.addEventListener('pause', () => setPlayPauseIcon(false));
video.addEventListener('timeupdate', () => { updateProgress(); scheduleProgressSave(); });
video.addEventListener('ended', () => {
  if (loopVideo) return;
  if (abPointA !== null && abPointB !== null) { video.currentTime = abPointA; video.play(); return; }
  playNext();
});

let videoClickTimer = null;
video.addEventListener('click', () => {
  clearTimeout(videoClickTimer);
  videoClickTimer = setTimeout(async () => {
    if (video.paused) await tryPlay();
    else video.pause();
  }, 280);
});

video.addEventListener('dblclick', (e) => {
  e.preventDefault();
  clearTimeout(videoClickTimer);
  toggleFullscreen();
});

video.addEventListener('error', () => {
  const err = video.error;
  const codes = { 1: 'Aborted', 2: 'Network error', 3: 'Decode error', 4: 'Source not supported' };
  toast('Video error: ' + (codes[err?.code] || 'Unknown') + ' — try MPV or another quality', 6000);
});

video.addEventListener('progress', () => updateBufferBar());

video.addEventListener('waiting', () => { nowPlaying.textContent = (currentMedia?.title || 'Buffering') + '…'; });
video.addEventListener('playing', () => { nowPlaying.textContent = currentMedia?.title || 'Playing'; });

$('#mute-btn').addEventListener('click', () => {
  video.muted = !video.muted;
  setMuteIcon(video.muted);
});

$('#volume-slider').addEventListener('input', (e) => setVolume(+e.target.value));

speedSelect.addEventListener('change', () => { video.playbackRate = parseFloat(speedSelect.value); });

qualitySelect.addEventListener('change', () => {
  if (qualitySelect.value && currentMedia?.source_url) {
    const pos = video.currentTime;
    playUrl(currentMedia.source_url, qualitySelect.value, pos);
  }
});

subtitleSelect.addEventListener('change', () => {
  const idx = subtitleSelect.value;
  video.querySelectorAll('track').forEach((t) => t.remove());
  if (idx === '' || !currentMedia?.subtitles) return;
  const sub = currentMedia.subtitles[idx];
  if (!sub?.proxy_url) return;
  const track = document.createElement('track');
  track.kind = 'subtitles';
  track.src = sub.proxy_url;
  track.srclang = sub.lang;
  track.label = sub.name;
  track.default = true;
  video.appendChild(track);
  track.addEventListener('load', () => { track.track.mode = 'showing'; });
});

function isMobileLayout() {
  return window.matchMedia('(max-width: 768px)').matches;
}

function setVideoFocus(on) {
  videoFocusMode = !!on;
  const app = $('.app');
  app?.classList.toggle('video-focus-mode', videoFocusMode);
  const focusBtn = $('#focus-btn');
  focusBtn?.classList.toggle('active', videoFocusMode);
  focusBtn?.setAttribute('aria-pressed', String(videoFocusMode));
  const exitBtn = $('#focus-exit-btn');
  if (exitBtn) exitBtn.hidden = !videoFocusMode;
  localStorage.setItem(FOCUS_KEY, videoFocusMode ? '1' : '0');
  if (videoFocusMode) setSidebarOpen(false);
}

function toggleVideoFocus() {
  setVideoFocus(!videoFocusMode);
}

function setSidebarOpen(open) {
  const sidebar = $('#sidebar');
  const backdrop = $('#sidebar-backdrop');
  if (!isMobileLayout()) {
    sidebar?.classList.remove('open');
    if (backdrop) backdrop.hidden = true;
    return;
  }
  sidebar?.classList.toggle('open', open);
  if (backdrop) backdrop.hidden = !open;
}

$('#sidebar-toggle')?.addEventListener('click', () => {
  const sidebar = $('#sidebar');
  setSidebarOpen(!sidebar?.classList.contains('open'));
});

$('#sidebar-backdrop')?.addEventListener('click', () => setSidebarOpen(false));

$('#mobile-more-btn')?.addEventListener('click', () => setSidebarOpen(true));

$$('.item-list').forEach((list) => {
  list.addEventListener('click', () => {
    if (isMobileLayout()) setSidebarOpen(false);
  });
});

window.addEventListener('resize', () => {
  if (!isMobileLayout()) setSidebarOpen(false);
});

async function togglePip() {
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else await video.requestPictureInPicture();
  } catch (_) { toast('PiP not available'); }
}

async function toggleFullscreen() {
  const wrap = $('#player-wrap');
  const v = video;

  if (isFullscreen()) {
    try {
      await (document.exitFullscreen?.() || document.webkitExitFullscreen?.());
    } catch (_) {}
    return;
  }

  if (typeof v.webkitEnterFullscreen === 'function') {
    try {
      v.webkitEnterFullscreen();
      return;
    } catch (_) {}
  }

  try {
    if (v.requestFullscreen) {
      await v.requestFullscreen();
      return;
    }
  } catch (_) {}

  try {
    if (wrap.requestFullscreen) await wrap.requestFullscreen();
    else if (wrap.webkitRequestFullscreen) wrap.webkitRequestFullscreen();
  } catch (_) {
    toast('Fullscreen not supported on this device', 3500);
  }
}

$('#pip-btn').addEventListener('click', () => togglePip());
$('#mobile-pip-btn')?.addEventListener('click', () => togglePip());
$('#fs-btn').addEventListener('click', () => toggleFullscreen());
$('#mobile-fs-btn')?.addEventListener('click', () => toggleFullscreen());

$('#fit-select').addEventListener('change', () => setFitMode($('#fit-select').value));
$$('.fit-btn').forEach((btn) => {
  btn.addEventListener('click', () => setFitMode(btn.dataset.fit));
});

['fullscreenchange', 'webkitfullscreenchange'].forEach((evt) => {
  document.addEventListener(evt, () => {
    const fs = isFullscreen();
    setFullscreenIcon(fs);
    $('#fs-fit-bar').style.display = 'none';
  });
});

$('#clear-queue-btn').addEventListener('click', async () => {
  await api('/api/queue', { method: 'DELETE' });
  queue = []; queueIndex = -1;
  refreshQueue();
});

$('#clear-history-btn').addEventListener('click', async () => {
  await api('/api/history', { method: 'DELETE' });
  refreshHistory();
});

$('#batch-btn').addEventListener('click', () => $('#batch-dialog').showModal());
$('#batch-dialog').addEventListener('close', async () => {
  if ($('#batch-dialog').returnValue !== 'ok') return;
  try {
    const res = await api('/api/batch', { method: 'POST', body: JSON.stringify({ urls: $('#batch-text').value }) });
    toast(`Queued ${res.queued} items`);
    await refreshQueue();
    if (queueIndex < 0 && queue.length) playQueueItem(0);
  } catch (e) { toast(e.message, 4000); }
});

$('#new-playlist-btn').addEventListener('click', async () => {
  const name = prompt('Playlist name:');
  if (!name) return;
  await api('/api/playlists', { method: 'POST', body: JSON.stringify({ name }) });
  refreshPlaylists();
});

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === `panel-${tab.dataset.tab}`));
    if (tab.dataset.tab === 'library') refreshLibrary();
    if (tab.dataset.tab === 'torrents') { refreshTorrents(); initPikpak(); initMpvSettings(); refreshMpvBridgeStatus(); }
  });
});

/* Drag & drop */
const dropZone = $('#drop-zone');
['dragenter', 'dragover'].forEach((ev) => {
  document.addEventListener(ev, (e) => { e.preventDefault(); dropZone.style.borderColor = 'var(--accent)'; });
});
document.addEventListener('dragleave', () => { dropZone.style.borderColor = ''; });
document.addEventListener('drop', async (e) => {
  e.preventDefault();
  dropZone.style.borderColor = '';
  const url = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
  if (url?.trim()) { playUrl(url.trim()); return; }
  const file = e.dataTransfer.files[0];
  if (file) {
    const fd = new FormData();
    fd.append('file', file);
    if (file.name.toLowerCase().endsWith('.torrent')) {
      toast('Adding torrent...', 5000);
      try {
        const res = await fetch('/api/torrent/add', { method: 'POST', body: fd }).then((r) => {
          if (!r.ok) return r.json().then((d) => Promise.reject(new Error(d.detail || 'Failed')));
          return r.json();
        });
        await refreshTorrents();
        const videoFile = (res.files || []).find((f) => f.is_video);
        if (videoFile) await playTorrent(res.id, videoFile.index);
        else toast('Torrent added');
      } catch (err) { toast(err.message, 5000); }
      return;
    }
    toast('Uploading...');
    const res = await fetch('/api/upload', { method: 'POST', body: fd }).then((r) => r.json());
    if (res.play_url) {
      if (res.play_url.startsWith('/api/torrent/') || res.type === 'torrent') {
        const m = res.play_url.match(/\/api\/torrent\/([^/]+)\/stream\/(\d+)/);
        if (m) await playTorrent(m[1], parseInt(m[2], 10));
        else if (res.torrent_id) await playTorrent(res.torrent_id);
      } else if (res.stream_url || res.type === 'pikpak') {
        await playUrl(res.url || res.source_url || urlInput.value);
      } else {
        playLocal(res.play_url, res.title);
      }
    }
    toast('Playing file');
    refreshLibrary();
  }
});

/* Clipboard paste */
document.addEventListener('paste', (e) => {
  if (e.target.matches('input, textarea')) return;
  const text = e.clipboardData.getData('text').trim();
  if (/^magnet:\?/i.test(text)) {
    urlInput.value = text;
    playUrl(text);
  } else if (/^https?:\/\//i.test(text)) {
    urlInput.value = text;
    playUrl(text);
  }
});

/* Keyboard shortcuts */
document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea, select')) return;
  const skip = () => e.preventDefault();

  switch (e.key) {
    case ' ':
    case 'k': case 'K':
      skip(); video.paused ? video.play() : video.pause(); break;
    case 'j': case 'J': skip(); video.currentTime = Math.max(0, video.currentTime - 10); break;
    case 'l': case 'L': skip(); video.currentTime = Math.min(getDuration(), video.currentTime + 10); break;
    case 'ArrowLeft': video.currentTime = Math.max(0, video.currentTime - (e.shiftKey ? 30 : 5)); break;
    case 'ArrowRight': video.currentTime = Math.min(getDuration(), video.currentTime + (e.shiftKey ? 30 : 5)); break;
    case 'ArrowUp': setVolume(Math.min(200, +$('#volume-slider').value + 5)); $('#volume-slider').value = Math.min(200, +$('#volume-slider').value + 5); break;
    case 'ArrowDown': setVolume(Math.max(0, +$('#volume-slider').value - 5)); $('#volume-slider').value = Math.max(0, +$('#volume-slider').value - 5); break;
    case 'f': case 'F': $('#fs-btn').click(); break;
    case 'c': case 'C': skip(); toggleVideoFocus(); break;
    case 'Escape':
      if (videoFocusMode) { skip(); setVideoFocus(false); }
      break;
    case 'v': case 'V': cycleFitMode(); break;
    case 'm': case 'M': $('#mute-btn').click(); break;
    case 'n': case 'N': playNext(); break;
    case 'p': case 'P': if (queueIndex > 0) playQueueItem(queueIndex - 1); break;
    case 'r': case 'R': $('#loop-btn').click(); break;
    case 's': case 'S': takeScreenshot(); break;
    case ',': video.pause(); video.currentTime = Math.max(0, video.currentTime - (1/30)); break;
    case '.': video.pause(); video.currentTime += (1/30); break;
    default:
      if (e.key >= '0' && e.key <= '9') {
        const dur = getDuration();
        if (dur) video.currentTime = (parseInt(e.key, 10) / 10) * dur;
      }
  }
});

$('#player-wrap').addEventListener('mousemove', () => {
  $('#player-wrap').classList.remove('hide-controls');
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    if (!video.paused && !isSeeking) $('#player-wrap').classList.add('hide-controls');
  }, 3000);
});

/* Init */
if (videoFocusMode) setVideoFocus(true);
setFitMode(fitMode);
setPlayPauseIcon(false);
setMuteIcon(false);
setFullscreenIcon(false);
initPlayerMenu();
connectWs();
initStatus();
initSettings();
refreshQueue();
refreshHistory();
refreshFavorites();
refreshDownloads();
refreshPlaylists();
refreshTorrents();
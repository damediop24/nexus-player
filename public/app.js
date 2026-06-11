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

const cloudStatus = $('#cloud-status');
const cloudStatusText = $('#cloud-status-text');
const cloudProgressBar = $('#cloud-progress-bar');

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
const MAX_BLOB_CACHE = 4 * 1024 * 1024 * 1024;  // allow larger full prefetches for "buffer till end"
const HLS_MAX_BUFFER_SEC = 72000;  // ~20h - try to buffer as much as possible / till end

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
let wantsPlayback = false;

function stringifyApiDetail(detail) {
  if (detail == null) return '';
  if (typeof detail === 'string') return detail;
  if (typeof detail === 'number' || typeof detail === 'boolean') return String(detail);
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && typeof item === 'object' && item.msg ? item.msg : stringifyApiDetail(item)))
      .filter(Boolean)
      .join('; ');
  }
  if (typeof detail === 'object') {
    const msg = detail.error ?? detail.message ?? detail.msg ?? detail.detail;
    if (typeof msg === 'string') return msg;
    if (msg != null) return stringifyApiDetail(msg);
    try { return JSON.stringify(detail); } catch (_) { return 'Request failed'; }
  }
  return String(detail);
}

function attachApiError(detail, status = 400) {
  const message = stringifyApiDetail(detail) || `Error ${status}`;
  const err = new Error(message);
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    err.code = detail.code || null;
    err.hint = typeof detail.hint === 'string' ? detail.hint : null;
    err.retriable = !!detail.retriable;
    err.site = detail.site || null;
  }
  return err;
}

function rejectApiResponse(res, data, fallback = 'Request failed') {
  return Promise.reject(attachApiError(data?.detail ?? data?.error ?? data ?? fallback, res?.status || 400));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw attachApiError(data.detail ?? data.error ?? data, res.status);
  return data;
}

function toast(msg, ms = 2800) {
  const el = $('#toast');
  el.textContent = typeof msg === 'string' ? msg : stringifyApiDetail(msg);
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

function primePlaybackGesture() {
  wantsPlayback = true;
  if (audioCtx?.state === 'suspended') audioCtx.resume().catch(() => {});
}

function showPlayPrompt(label = 'Tap to play') {
  const el = $('#play-prompt');
  const text = el?.querySelector('.play-prompt-text');
  if (text) text.textContent = label;
  if (el) el.hidden = false;
}

function hidePlayPrompt() {
  const el = $('#play-prompt');
  if (el) el.hidden = true;
}

function tryPlayFromGesture() {
  primePlaybackGesture();
  if (audioCtx?.state === 'suspended') audioCtx.resume().catch(() => {});
  return video.play()
    .then(() => {
      hidePlayPrompt();
      setPlayPauseIcon(true);
      wantsPlayback = false;
      return true;
    })
    .catch(() => {
      showPlayPrompt();
      setPlayPauseIcon(false);
      return false;
    });
}

async function tryPlay() {
  if (audioCtx?.state === 'suspended') {
    await audioCtx.resume().catch(() => {});
  }
  try {
    await video.play();
    hidePlayPrompt();
    wantsPlayback = false;
    return true;
  } catch (_) {
    if (!video.muted) {
      try {
        video.muted = true;
        await video.play();
        hidePlayPrompt();
        setPlayPauseIcon(true);
        toast('Playing muted — adjust volume to unmute', 3500);
        return true;
      } catch (_) {
        video.muted = false;
      }
    }
    showPlayPrompt(wantsPlayback ? 'Tap to play' : 'Tap to play');
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
    // Strong "buffer till the end" prebuffering for direct progressive links (shemale6, erome, etc.)
    // Prioritize full file download via OPFS (large files) or Blob so the video is fully local and plays smoothly.
    // This optimizes prefetch to complete 100% and swap to local blob for zero server dependency after initial load.
    if (await hasOpfs()) {
      await prefetchToOpfs(url, total, signal);
      return;
    }
    if (!total || total <= MAX_BLOB_CACHE) {
      await prefetchToBlob(url, total, signal);
      return;
    }
    await prefetchProgressOnly(url, total, signal);
    setPrefetchPercent(100);  // force "buffered till end" indicator
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

const BROWSER_NATIVE_VIDEO_EXTS = new Set(['mp4', 'webm', 'ogv', 'm4v', 'm4p', '3gp', '3g2', 'm3u8']);

function streamExtFromInfo(info) {
  const fmt = info?.formats?.find((f) => f.format_id === info?.best_format_id) || info?.formats?.[0];
  return (fmt?.ext || '').toLowerCase();
}

function needsMpvInBrowser(ext) {
  if (!ext || ext === 'm3u8' || ext === 'mpd') return false;
  return !BROWSER_NATIVE_VIDEO_EXTS.has(ext);
}

function needsMpvForCodec(info) {
  return false; // MPV support removed
}

let blackVideoWatch = null;

function clearBlackVideoWatch() {
  if (blackVideoWatch) {
    clearInterval(blackVideoWatch);
    blackVideoWatch = null;
  }
}

function startBlackVideoWatch() {
  clearBlackVideoWatch();
  let triggered = false;
  blackVideoWatch = setInterval(() => {
    if (triggered || !currentMedia || video.paused) return;
    if (video.readyState < 2 || !(video.duration > 0) || video.currentTime < 1.5) return;
    if (video.videoWidth === 0 && video.videoHeight === 0) {
      triggered = true;
      clearBlackVideoWatch();
      toast('No picture in browser (codec not supported) — try another quality or direct link.', 9000);
    }
  }, 600);
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
  const ext = streamExtFromInfo(currentMedia);
  const hint = needsMpvInBrowser(ext)
    ? `Browsers cannot play .${ext} reliably — try a different quality or direct .mp4/.m3u8 link.`
    : 'Try another quality or a direct .mp4/.m3u8 link.';
  handlePlaybackError(detail, {
    message: detail,
    hint,
    retriable: true,
  });
  if (code === 3 || code === 4 || needsMpvForCodec(currentMedia)) {
    // MPV removed - suggest direct link or quality
  }
}

function isXvideosMedia(info) {
  const site = (info?.site || '').toLowerCase();
  const src = (info?.source_url || info?.url || '').toLowerCase();
  return site === 'xvideos' || src.includes('xvideos.com');
}

function buildHlsConfig() {
  const base = {
    enableWorker: true,
    maxMaxBufferLength: HLS_MAX_BUFFER_SEC,
    maxBufferSize: 32 * 1024 * 1024 * 1024,  // larger for strong buffering
    backBufferLength: 600,
    progressive: true,
    startFragPrefetch: true,
    maxLoadingDelay: 0,
    maxStarvationDelay: 2,
  };
  const dur = Number(currentMedia?.duration) || 0;
  if (!isXvideosMedia(currentMedia)) {
    // Strong prebuffering: aim to buffer as much as possible / till end
    const target = dur > 0 ? Math.ceil(dur) + 600 : 7200;  // at least 2h or full+margin
    return { ...base, maxBufferLength: Math.max(target, 3600) };
  }
  return {
    ...base,
    maxBufferLength: Math.max(dur + 300, 7200),
    testBandwidth: false,
    capLevelToPlayerSize: false,
    abrEwmaDefaultEstimate: 50000000,
  };
}

function loadSource(url, type = 'progressive') {
  clearBlackVideoWatch();
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
        hint: 'Try another quality or a direct link.',
        retriable: true,
      });
    });
  } else if (type === 'hls' && typeof Hls !== 'undefined' && Hls.isSupported()) {
    const xvideosHls = isXvideosMedia(currentMedia);
    hls = new Hls(buildHlsConfig());
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (xvideosHls) {
        const dur = hls.media?.duration || Number(currentMedia?.duration) || 0;
        if (dur > 0) {
          hls.config.maxBufferLength = Math.ceil(dur) + 120;
          hls.config.maxMaxBufferLength = Math.max(hls.config.maxMaxBufferLength, hls.config.maxBufferLength);
        }
        try { hls.startLoad(-1); } catch (_) {}
      }
      tryPlay();
    });
    if (xvideosHls) {
      hls.on(Hls.Events.LEVEL_LOADED, (_, data) => {
        hls._nexusTotalFrags = data.details?.totalFragments || data.details?.fragments?.length || 0;
        hls._nexusBufferedFrags = 0;
      });
    }
    hls.on(Hls.Events.BUFFER_APPENDED, () => updateBufferBar());
    hls.on(Hls.Events.FRAG_BUFFERED, (_, data) => {
      if (xvideosHls && hls._nexusTotalFrags) {
        const sn = data?.frag?.sn;
        if (typeof sn === 'number') {
          hls._nexusBufferedFrags = Math.max(hls._nexusBufferedFrags || 0, sn + 1);
          setPrefetchPercent((hls._nexusBufferedFrags / hls._nexusTotalFrags) * 100);
        }
      }
      updateBufferBar();
    });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (!data.fatal) return;
      const details = stringifyApiDetail(data.details) || stringifyApiDetail(data.type) || 'HLS playback failed';
      const canFallbackMp4 = currentMedia?.formats?.some((f) => (
        f.format_id?.startsWith('mp4')
        && f.format_id !== currentMedia.best_format_id
        && !(f.url || '').includes('.m3u8')
      ));
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR && !hls._nexusRetried) {
        hls._nexusRetried = true;
        try {
          hls.startLoad(-1);
          return;
        } catch (_) {}
      }
      if (canFallbackMp4 && !hls._nexusMp4Fallback) {
        hls._nexusMp4Fallback = true;
        const mp4Fmt = currentMedia.formats.find((f) => f.format_id?.startsWith('mp4-high'))
          || currentMedia.formats.find((f) => f.format_id?.startsWith('mp4'));
        if (mp4Fmt?.format_id && currentMedia.source_url) {
          toast('HLS failed — trying MP4 quality…', 5000);
          playUrl(currentMedia.source_url, mp4Fmt.format_id);
          return;
        }
      }
      const hint = data.type === Hls.ErrorTypes.NETWORK_ERROR
        ? 'Network error — stream may have expired. Replay the link or try another quality.'
        : 'Try another quality or a direct link.';
      handlePlaybackError('HLS stream error', {
        message: details,
        hint,
        retriable: true,
      });
    });
  } else if (type === 'hls' && video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    video.addEventListener('loadedmetadata', () => tryPlay(), { once: true });
  } else {
    loadProgressive(url);
    video.addEventListener('loadedmetadata', () => {
      if (video.duration > 0 && video.duration < 1 && video.seekable.length) {
        toast('Stream looks invalid — try another quality or direct link', 6000);
      }
    }, { once: true });
  }
}



function formatAlldebridPhase(phase) {
  const labels = {
    running: 'Downloading to cloud',
    pending: 'Queued',
    complete: 'Ready in cloud',
    error: 'Failed',
  };
  return labels[phase] || phase || 'Cloud';
}





function findMatchingAlldebridTask(tasks, info) {
  if (!tasks?.length || !info) return null;
  
  if (info.task_id) {
    const byTask = tasks.find((t) => t.task_id === info.task_id || t.id === info.task_id);
    if (byTask) return byTask;
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
    'AllDebrid',
    formatAlldebridPhase(task.phase),
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
  if (!appStatus?.alldebrid?.configured || !isAlldebridMedia(currentMedia)) {
    cloudStatus.classList.add('hidden');
    return;
  }
  try {
    const tasks = await api('/api/alldebrid/tasks');
    renderCloudStatus(findMatchingAlldebridTask(tasks, currentMedia) || tasks[0]);
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
  if (isAlldebridMedia(info)) updateCloudStatus();
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
  if (isAlldebridMedia(info)) updateCloudStatus();
  cloudPollActive = isAlldebridMedia(info);
}













async 

function shouldPollCloudStatus() {
  return appStatus?.alldebrid?.configured && (
    cloudPollActive
    || isAlldebridMedia(currentMedia)
  );
}





function isBlockedSiteError(msg) {
  return /403|forbidden|blocked this server|cloud servers|sign in|confirm your age|bot|captcha|unable to extract/i.test(msg || '');
}

function shouldTryLocalBridge(err) {
  return false; // MPV support removed
}

function formatPlayError(err) {
  let message = 'Playback failed';
  if (typeof err === 'string') message = err;
  else if (err instanceof Error) message = stringifyApiDetail(err.message) || err.message || message;
  else if (err?.message != null) message = stringifyApiDetail(err.message) || message;
  else if (err != null) message = stringifyApiDetail(err) || message;
  const parts = [message];
  if (typeof err?.hint === 'string' && err.hint) parts.push(err.hint);
  return parts.join(' — ');
}

function handlePlaybackError(context, err) {
  const msg = formatPlayError(err);
  toast(`${context}: ${msg}`, 9000);
  if (nowPlaying) nowPlaying.textContent = 'Playback error';
}





function applyPlayback(info, resumePos = 0) {
  currentMedia = info;
  overlay.classList.add('hidden');
  setMeta(info);
  populateFormats(info.formats || [], info.best_format_id);
  populateSubtitles(info.subtitles || []);

  if (needsMpvForCodec(info)) {
    toast('H.265/MKV video may need external player — try another quality or direct link.', 8000);
    recordPlayback(info);
    wsSend({ cmd: 'nowplaying', title: info.title, url: info.source_url });
    return;
  }

  wantsPlayback = true;
  loadSource(info.play_url, info.stream_type);
  startBlackVideoWatch();

  const seekTo = () => {
    if (resumePos > 0 && getDuration()) video.currentTime = resumePos;
  };
  video.addEventListener('loadedmetadata', seekTo, { once: true });
  video.addEventListener('durationchange', seekTo, { once: true });

  recordPlayback(info);
  toast('Playing: ' + (info.title || 'media'));
  wsSend({ cmd: 'nowplaying', title: info.title, url: info.source_url });
  if (isAlldebridMedia(info)) updateCloudStatus();
  
}



async function playUrl(url, formatId = null, resumePos = 0) {
  if (!url) return;
  if (formatId == null) qualitySelect.value = '';
  wantsPlayback = true;
  toast('Resolving stream...');
  urlInput.value = url;

  try {
    const info = await api('/api/play', {
      method: 'POST',
      body: JSON.stringify({ url, format_id: formatId || null }),
    });

    if (info.type === 'playlist') {
      const count = info.queued || info.entries?.length || 0;
      toast(`Queued ${count} items — starting first video…`);
      await refreshQueue();
      const first = info.entries?.[0];
      if (first?.url) {
        queueIndex = 0;
        await playUrl(first.url, first.format_id, resumePos);
        return;
      }
      if (queue.length) {
        queueIndex = 0;
        await playQueueItem(0);
      }
      return;
    }

    applyPlayback(info, resumePos);
  } catch (e) {
    if (shouldTryLocalBridge(e)) {
      handlePlaybackError('Blocked on cloud server', {
        message: stringifyApiDetail(e.message) || 'Cloud resolve failed',
        hint: e.hint || 'Try direct link, another quality, or local player.',
        code: e.code,
      });
      return;
    }
    handlePlaybackError('Could not play link', e);
  } finally {
    cloudPollActive = isAlldebridMedia(currentMedia);
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

   else {

  }
  await initAlldebrid();

  await syncLibrary();
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
$('#play-btn').addEventListener('click', () => {
  primePlaybackGesture();
  playUrl(urlInput.value.trim());
});
$('#play-prompt')?.addEventListener('click', (e) => {
  e.stopPropagation();
  tryPlayFromGesture().then((ok) => { if (ok) wsSend({ cmd: 'play' }); });
});
$('#focus-btn')?.addEventListener('click', toggleVideoFocus);
$('#focus-exit-btn')?.addEventListener('click', toggleVideoFocus);

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



async function launchResolvedInMpv(absUrl, title, referer = '') {

  const bridgeUrl = buildMpvBridgeGetUrl(absUrl, title, referer);
  const useForm = bridgeUrl.length > 5500 || absUrl.length > 1800;

  // Check if bridge is actually running before trying to open the raw /launch URL
  // (prevents opening a useless tab with the launch URL when bridge is not started)
  let bridgeRunning = false;
  try {
    const health = await fetch(`http://127.0.0.1:${MPV_BRIDGE_PORT}/health`, {
      signal: AbortSignal.timeout(1500)
    });
    bridgeRunning = health.ok;
  } catch (_) {
    bridgeRunning = false;
  }

  if (bridgeRunning && !useForm) {
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
  toast('MPV launcher ready — click Launch MPV in the dialog if the player did not open. Make sure start-mpv-bridge.vbs is running on this PC.', 8000);
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

  // For cloud-proxied streams (e.g. shemale6 on Railway), prefer re-resolving the original source
  // via the local bridge so MPV gets a direct/full stream that local network can play reliably
  // (cloud proxy may be limited or only have preview for some sites).
  const isCloudProxy = absUrl.includes('/api/proxy/') && (currentMedia?.site === 'shemale6' || /shemale6|blocked|cloud/i.test(currentMedia?.source_url || ''));
  if (isCloudProxy) {
    try {
      const source = currentMedia?.source_url || urlInput.value.trim();
      if (source) {
        const resolved = await resolveViaLocalBridge(source);
        if (resolved && resolved.stream_url) {
          absUrl = resolved.stream_url;
          referer = resolved.headers?.Referer || referer;
          title = resolved.title || title;
        }
      }
    } catch (_) {
      // fall back to the cloud proxy URL
    }
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


$('#toggle-btn').addEventListener('click', () => {
  if (video.paused) {
    tryPlayFromGesture().then((ok) => { if (ok) wsSend({ cmd: 'play' }); });
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

video.addEventListener('play', () => {
  hidePlayPrompt();
  setPlayPauseIcon(true);
});
video.addEventListener('pause', () => setPlayPauseIcon(false));

video.addEventListener('timeupdate', () => { updateProgress(); scheduleProgressSave(); });
video.addEventListener('ended', () => {
  if (loopVideo) return;
  if (abPointA !== null && abPointB !== null) { video.currentTime = abPointA; video.play(); return; }
  playNext();
});

video.addEventListener('click', () => {
  if ($('#play-prompt')?.hidden === false) return;
  if (video.paused) tryPlayFromGesture();
  else video.pause();
});

video.addEventListener('dblclick', (e) => {
  e.preventDefault();
  toggleFullscreen();
});

video.addEventListener('error', () => {
  const err = video.error;
  const codes = { 1: 'Aborted', 2: 'Network error', 3: 'Decode error', 4: 'Source not supported' };
  toast('Video error: ' + (codes[err?.code] || 'Unknown') + ' — try another quality or direct link', 6000);
});

video.addEventListener('progress', () => updateBufferBar());

video.addEventListener('waiting', () => { nowPlaying.textContent = (currentMedia?.title || 'Buffering') + '…'; });
video.addEventListener('playing', () => {
  hidePlayPrompt();
  nowPlaying.textContent = currentMedia?.title || 'Playing';
});

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
    ).then((r) => {
          if (!r.ok) return r.json().then((d) => rejectApiResponse(r, d, 'Failed'));
          return r.json();
        });
        await 
        const videoFile = (res.files || []).find((f) => f.is_video);
        toast('Torrent support removed');
      } catch (err) { toast(err.message, 5000); }
      return;
    }
    toast('Uploading...');
    const res = await fetch('/api/upload', { method: 'POST', body: fd }).then((r) => r.json());
    if (res.play_url) {
       else if (res.stream_url || res.type === 'alldebrid') {
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
  if (/^https?:\/\//i.test(text)) {
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

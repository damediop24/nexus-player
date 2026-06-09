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

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `Error ${res.status}`);
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
    toast('Playback blocked — click the video or ▶ button to start', 5000);
    $('#toggle-btn').textContent = '▶';
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
}

function loadSource(url, type = 'progressive') {
  destroyHls();
  destroyDash();
  video.removeAttribute('src');
  video.load();

  if (type === 'dash' && typeof dashjs !== 'undefined') {
    dashPlayer = dashjs.MediaPlayer().create();
    dashPlayer.initialize(video, url, true);
    video.addEventListener('canplay', () => tryPlay(), { once: true });
  } else if (type === 'hls' && typeof Hls !== 'undefined' && Hls.isSupported()) {
    hls = new Hls({
      enableWorker: true,
      maxBufferLength: 60,
      maxMaxBufferLength: 120,
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => tryPlay());
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) toast('Stream error — try MPV or another quality', 4000);
    });
  } else if (type === 'hls' && video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    video.addEventListener('loadedmetadata', () => tryPlay(), { once: true });
  } else {
    video.src = url;
    video.addEventListener('canplay', () => tryPlay(), { once: true });
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
}

function populateSubtitles(subs) {
  subtitleSelect.innerHTML = '<option value="">No subtitles</option>';
  subs.forEach((s, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = s.name || s.lang;
    subtitleSelect.appendChild(opt);
  });
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

  if (isFinite(video.duration) && video.buffered.length) {
    const buf = (video.buffered.end(video.buffered.length - 1) / video.duration) * 100;
    progressBuffered.style.width = buf + '%';
  }

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

async function playUrl(url, formatId = null, resumePos = 0) {
  if (!url) return;
  toast('Resolving stream...');
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

    toast('Playing: ' + (info.title || 'media'));
    wsSend({ cmd: 'nowplaying', title: info.title, url: info.source_url });
  } catch (e) {
    const msg = e.message || String(e);
    toast('Error: ' + msg, 6000);
    if (/403|forbidden|blocked/i.test(msg)) {
      toast('Tip: Try the MPV button, or log into the site in Chrome/Edge first', 8000);
    }
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
  renderList(historyList, await api('/api/history'), (item) => playUrl(item.url, item.format_id, item.position || 0));
}

async function refreshFavorites() {
  renderList(favoritesList, await api('/api/favorites'), (item) => playUrl(item.url));
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
  const st = await api('/api/status');
  const remote = `http://${st.lan_ip}:${st.port}`;
  $('#remote-url').textContent = remote + ' (tap to copy)';
  $('#remote-url').onclick = () => { navigator.clipboard.writeText(remote); toast('Remote URL copied'); };
  if (!st.mpv_available) $('#mpv-btn').style.opacity = '0.4';
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
  } catch (e) { toast(e.message, 4000); }
});

$('#mpv-btn').addEventListener('click', async () => {
  const url = urlInput.value.trim() || currentMedia?.source_url;
  if (!url) return;
  try {
    await api('/api/mpv', { method: 'POST', body: JSON.stringify({ url }) });
    toast('Launched in MPV');
  } catch (e) { toast(e.message, 4000); }
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

$('#fav-btn').addEventListener('click', async () => {
  if (!currentMedia?.source_url) return;
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
    toast('Added to favorites');
    refreshFavorites();
  } catch (e) { toast(e.message); }
});

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
  toast(shuffleQueue ? 'Shuffle on' : 'Shuffle off');
});

$('#repeat-btn').addEventListener('click', () => {
  repeatQueue = !repeatQueue;
  $('#repeat-btn').classList.toggle('active', repeatQueue);
  toast(repeatQueue ? 'Repeat queue on' : 'Repeat queue off');
});

$('#ab-btn').addEventListener('click', toggleAbLoop);
$('#screenshot-btn').addEventListener('click', takeScreenshot);
$('#refresh-library-btn')?.addEventListener('click', refreshLibrary);

video.addEventListener('play', () => { $('#toggle-btn').textContent = '⏸'; });
video.addEventListener('pause', () => { $('#toggle-btn').textContent = '▶'; });
video.addEventListener('timeupdate', () => { updateProgress(); scheduleProgressSave(); });
video.addEventListener('ended', () => {
  if (loopVideo) return;
  if (abPointA !== null && abPointB !== null) { video.currentTime = abPointA; video.play(); return; }
  playNext();
});

video.addEventListener('click', async () => {
  if (video.paused) await tryPlay();
  else video.pause();
});

video.addEventListener('error', () => {
  const err = video.error;
  const codes = { 1: 'Aborted', 2: 'Network error', 3: 'Decode error', 4: 'Source not supported' };
  toast('Video error: ' + (codes[err?.code] || 'Unknown') + ' — try MPV or another quality', 6000);
});

video.addEventListener('waiting', () => { nowPlaying.textContent = (currentMedia?.title || 'Loading') + '…'; });
video.addEventListener('playing', () => { nowPlaying.textContent = currentMedia?.title || 'Playing'; });

$('#mute-btn').addEventListener('click', () => {
  video.muted = !video.muted;
  $('#mute-btn').textContent = video.muted ? '🔇' : '🔊';
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

$('#pip-btn').addEventListener('click', async () => {
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else await video.requestPictureInPicture();
  } catch (_) { toast('PiP not available'); }
});

$('#fs-btn').addEventListener('click', () => {
  const wrap = $('#player-wrap');
  document.fullscreenElement ? document.exitFullscreen() : wrap.requestFullscreen();
});

$('#fit-select').addEventListener('change', () => setFitMode($('#fit-select').value));
$$('.fit-btn').forEach((btn) => {
  btn.addEventListener('click', () => setFitMode(btn.dataset.fit));
});

document.addEventListener('fullscreenchange', () => {
  const fs = !!document.fullscreenElement;
  $('#fs-fit-bar').style.display = fs ? 'flex' : '';
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
    toast('Uploading...');
    const res = await fetch('/api/upload', { method: 'POST', body: fd }).then((r) => r.json());
    playLocal(res.play_url, res.title);
    toast('Playing local file');
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
setFitMode(fitMode);
connectWs();
initStatus();
initSettings();
refreshQueue();
refreshHistory();
refreshFavorites();
refreshDownloads();
refreshPlaylists();
/*
 * AxonOS desktop file transfer panel.
 *
 * Talks to the same-origin gate routes /api/files/* which proxy to the file
 * agent inside this wallet's desktop container. Built for large files:
 *  - uploads are chunked (32 MB) and resumable: the server keeps a partial
 *    file, so a dropped connection, page reload, or pause continues from the
 *    last byte that reached the desktop; chunks retry with backoff forever
 *    until cancelled.
 *  - downloads are plain authenticated GETs with Range support, so the
 *    browser's native download manager handles them (pause/resume/retry,
 *    no memory buffering in the page).
 *
 * Loaded lazily by ui.js when the Files deck button is first opened.
 */

const CHUNK_BYTES = (window.AXONOS_FILES_CHUNK_BYTES | 0) > 0
    ? (window.AXONOS_FILES_CHUNK_BYTES | 0)
    : 1000 * 1024;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 60 * 1000;

let _bound = false;
let _currentDir = '';
let _transfers = [];
let _nextTransferId = 1;
let _uploadRunning = false;

function _wallet() {
    return (window.verifiedWalletAddress || '').trim();
}

function _authHeaders() {
    const headers = {};
    const wallet = _wallet();
    if (wallet) {
        headers['X-Wallet-Address'] = wallet;
    }
    if (window.verifiedAuthToken) {
        headers['X-AXGT-Auth-Token'] = window.verifiedAuthToken;
    }
    return headers;
}

function _apiUrl(route, params) {
    const qs = new URLSearchParams(params || {});
    qs.set('wallet', _wallet());
    return `./api/files/${route}?${qs.toString()}`;
}

async function _apiJson(route, params, options) {
    const resp = await fetch(_apiUrl(route, params), {
        credentials: 'same-origin',
        headers: _authHeaders(),
        ...(options || {}),
    });
    let data = null;
    try {
        data = await resp.json();
    } catch (e) {
        data = null;
    }
    return { status: resp.status, ok: resp.ok, data };
}

function _el(id) {
    return document.getElementById(id);
}

function _fmtBytes(n) {
    if (!Number.isFinite(n) || n < 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function _fmtEta(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return '';
    if (seconds < 60) return `${Math.ceil(seconds)}s left`;
    if (seconds < 3600) return `${Math.ceil(seconds / 60)}m left`;
    return `${(seconds / 3600).toFixed(1)}h left`;
}

function _setStatus(text, isError) {
    const el = _el('axonos_files_status');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('axonos-files-status--error', !!isError);
}

function _joinPath(dir, name) {
    return dir ? `${dir}/${name}` : name;
}

/* ------------------------------------------------------------------ *
 * Directory listing
 * ------------------------------------------------------------------ */

async function refreshListing() {
    if (!_wallet()) {
        _setStatus('Connect and verify a wallet first.', true);
        return;
    }
    _setStatus('Loading…');
    let res;
    try {
        res = await _apiJson('list', { path: _currentDir });
    } catch (e) {
        _setStatus('Could not reach the desktop file agent.', true);
        return;
    }
    if (!res.ok || !res.data || !res.data.ok) {
        _setStatus((res.data && res.data.error) || `Listing failed (${res.status})`, true);
        return;
    }
    _currentDir = res.data.path || '';
    _el('axonos_files_path').textContent = '/' + _currentDir;
    _el('axonos_files_up').disabled = !_currentDir;

    const list = _el('axonos_files_list');
    list.textContent = '';
    for (const entry of res.data.entries) {
        list.appendChild(_renderEntry(entry));
    }
    if (!res.data.entries.length) {
        const empty = document.createElement('li');
        empty.className = 'axonos-files-empty';
        empty.textContent = 'Empty folder — drop files here to upload.';
        list.appendChild(empty);
    }
    _setStatus(`Free space on desktop: ${_fmtBytes(res.data.disk_free_bytes)}`);
}

function _renderEntry(entry) {
    const li = document.createElement('li');
    li.className = `axonos-files-entry axonos-files-entry--${entry.type}`;

    const name = document.createElement('span');
    name.className = 'axonos-files-entry__name';
    name.textContent = (entry.type === 'dir' ? '📁 ' : '📄 ') + entry.name;
    name.title = entry.name;
    li.appendChild(name);

    if (entry.type === 'dir') {
        name.addEventListener('click', () => {
            _currentDir = _joinPath(_currentDir, entry.name);
            refreshListing();
        });
    } else {
        const size = document.createElement('span');
        size.className = 'axonos-files-entry__size';
        size.textContent = _fmtBytes(entry.size);
        li.appendChild(size);

        const dl = document.createElement('button');
        dl.type = 'button';
        dl.className = 'axonos-files-entry__dl';
        dl.title = 'Download to this device (browser handles pause/resume)';
        dl.textContent = '⬇';
        dl.addEventListener('click', () => downloadFile(_joinPath(_currentDir, entry.name)));
        li.appendChild(dl);
    }
    return li;
}

/* ------------------------------------------------------------------ *
 * Downloads — native browser download manager via authenticated GET
 * ------------------------------------------------------------------ */

function downloadFile(relPath) {
    const params = { path: relPath };
    // Cookie auth is primary; mirror the websockify URL fallback for
    // deployments where the HttpOnly cookie is unavailable.
    if (window.verifiedAuthToken) {
        params.auth_token = window.verifiedAuthToken;
    }
    const a = document.createElement('a');
    a.href = _apiUrl('download', params);
    a.download = relPath.split('/').pop();
    document.body.appendChild(a);
    a.click();
    a.remove();
}

/* ------------------------------------------------------------------ *
 * Uploads — chunked, resumable, retry-forever
 * ------------------------------------------------------------------ */

function enqueueUploads(fileList) {
    const files = Array.from(fileList || []).filter((f) => f && f.name);
    if (!files.length) return;
    for (const file of files) {
        _transfers.push({
            id: _nextTransferId++,
            file,
            relPath: _joinPath(_currentDir, file.name),
            total: file.size,
            offset: 0,
            state: 'queued',
            speed: 0,
            error: '',
            abort: null,
            overwrite: false,
        });
    }
    _renderTransfers();
    _pumpUploads();
}

async function _pumpUploads() {
    if (_uploadRunning) return;
    _uploadRunning = true;
    try {
        for (;;) {
            const next = _transfers.find((t) => t.state === 'queued');
            if (!next) break;
            await _runUpload(next);
        }
    } finally {
        _uploadRunning = false;
    }
}

async function _runUpload(t) {
    t.state = 'uploading';
    t.error = '';
    _renderTransfers();
    let attempt = 0;

    for (;;) {
        if (t.state === 'cancelled' || t.state === 'paused') return;
        try {
            const status = await _apiJson('upload-status', { path: t.relPath, total: t.total });
            if (!status.ok || !status.data || !status.data.ok) {
                throw new Error((status.data && status.data.error) || `status ${status.status}`);
            }
            t.offset = status.data.offset || 0;
            if (status.data.exists && t.offset === 0 && !t.overwrite) {
                const replace = window.confirm(
                    `"${t.relPath}" already exists on the desktop. Replace it?`);
                if (!replace) {
                    t.state = 'cancelled';
                    _renderTransfers();
                    return;
                }
                t.overwrite = true;
            }

            while (t.offset < t.total || t.total === 0) {
                if (t.state === 'cancelled' || t.state === 'paused') return;
                const end = Math.min(t.offset + CHUNK_BYTES, t.total);
                const params = { path: t.relPath, offset: t.offset, total: t.total };
                if (t.offset === 0 && t.overwrite) params.overwrite = 1;
                const controller = new AbortController();
                t.abort = controller;
                const sentAt = Date.now();
                const resp = await fetch(_apiUrl('upload', params), {
                    method: 'PUT',
                    credentials: 'same-origin',
                    headers: { ..._authHeaders(), 'Content-Type': 'application/octet-stream' },
                    body: t.file.slice(t.offset, end),
                    signal: controller.signal,
                });
                t.abort = null;
                let data = null;
                try { data = await resp.json(); } catch (e) { /* non-JSON error body */ }
                if (resp.status === 409 && data && Number.isFinite(data.offset)) {
                    // Server-side partial diverged (e.g. parallel tab) — realign.
                    t.offset = data.offset;
                    continue;
                }
                if (resp.status === 409 && data && data.error === 'exists') {
                    t.state = 'error';
                    t.error = 'File exists on desktop';
                    _renderTransfers();
                    return;
                }
                if (resp.status === 413 || resp.status === 507) {
                    t.state = 'error';
                    t.error = (data && data.error) || 'Desktop rejected the file';
                    _renderTransfers();
                    return;
                }
                if (!resp.ok || !data || !data.ok) {
                    throw new Error((data && data.error) || `upload chunk failed (${resp.status})`);
                }
                const sentBytes = data.offset - t.offset;
                const elapsed = Math.max(0.05, (Date.now() - sentAt) / 1000);
                t.speed = t.speed ? t.speed * 0.6 + (sentBytes / elapsed) * 0.4 : sentBytes / elapsed;
                t.offset = data.offset;
                attempt = 0;
                _renderTransfers();
                if (data.complete) break;
            }
            t.state = 'done';
            _renderTransfers();
            refreshListing();
            return;
        } catch (err) {
            if (t.state === 'cancelled' || t.state === 'paused') return;
            if (err && err.name === 'AbortError') return;
            attempt += 1;
            const wait = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** Math.min(attempt, 6));
            t.error = `Connection problem — retrying in ${Math.round(wait / 1000)}s (kept ${_fmtBytes(t.offset)})`;
            _renderTransfers();
            await new Promise((r) => { t._retryTimer = setTimeout(r, wait); });
            t.error = '';
        }
    }
}

function _pauseTransfer(t) {
    if (t.state !== 'uploading') return;
    t.state = 'paused';
    if (t.abort) t.abort.abort();
    if (t._retryTimer) clearTimeout(t._retryTimer);
    _renderTransfers();
}

function _resumeTransfer(t) {
    if (t.state !== 'paused' && t.state !== 'error') return;
    t.state = 'queued';
    t.error = '';
    _renderTransfers();
    _pumpUploads();
}

function _cancelTransfer(t) {
    const wasActive = t.state === 'uploading';
    t.state = 'cancelled';
    if (t.abort) t.abort.abort();
    if (t._retryTimer) clearTimeout(t._retryTimer);
    // Drop the server-side partial; ignore failures (it expires harmlessly).
    _apiJson('cancel-upload', { path: t.relPath }, { method: 'POST' }).catch(() => {});
    if (!wasActive) {
        _transfers = _transfers.filter((x) => x.id !== t.id);
    }
    _renderTransfers();
}

function _renderTransfers() {
    const host = _el('axonos_files_transfers');
    if (!host) return;
    host.textContent = '';
    for (const t of _transfers) {
        if (t.state === 'done' || t.state === 'cancelled') continue;
        const row = document.createElement('div');
        row.className = 'axonos-transfer';

        const name = document.createElement('div');
        name.className = 'axonos-transfer__name';
        name.textContent = t.relPath;
        row.appendChild(name);

        const bar = document.createElement('div');
        bar.className = 'axonos-transfer__bar';
        const fill = document.createElement('div');
        fill.className = 'axonos-transfer__fill';
        const pct = t.total > 0 ? (t.offset / t.total) * 100 : 100;
        fill.style.width = `${pct.toFixed(1)}%`;
        bar.appendChild(fill);
        row.appendChild(bar);

        const meta = document.createElement('div');
        meta.className = 'axonos-transfer__meta';
        const remaining = t.speed > 0 ? (t.total - t.offset) / t.speed : NaN;
        const bits = [`${_fmtBytes(t.offset)} / ${_fmtBytes(t.total)}`];
        if (t.state === 'uploading' && t.speed > 0) {
            bits.push(`${_fmtBytes(t.speed)}/s`, _fmtEta(remaining));
        }
        if (t.state === 'paused') bits.push('paused');
        if (t.error) bits.push(t.error);
        meta.textContent = bits.filter(Boolean).join(' · ');
        row.appendChild(meta);

        const controls = document.createElement('div');
        controls.className = 'axonos-transfer__controls';
        if (t.state === 'uploading') {
            controls.appendChild(_ctrlButton('Pause', () => _pauseTransfer(t)));
        }
        if (t.state === 'paused' || t.state === 'error') {
            controls.appendChild(_ctrlButton('Resume', () => _resumeTransfer(t)));
        }
        controls.appendChild(_ctrlButton('Cancel', () => _cancelTransfer(t)));
        row.appendChild(controls);

        host.appendChild(row);
    }
}

function _ctrlButton(label, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'axonos-transfer__btn';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
}

/* ------------------------------------------------------------------ *
 * Panel wiring (called by ui.js)
 * ------------------------------------------------------------------ */

export function onPanelOpen() {
    _bindOnce();
    refreshListing();
}

export function hasActiveTransfers() {
    return _transfers.some((t) => t.state === 'uploading' || t.state === 'queued');
}

function _bindOnce() {
    if (_bound) return;
    _bound = true;

    _el('axonos_files_refresh').addEventListener('click', refreshListing);
    _el('axonos_files_up').addEventListener('click', () => {
        const idx = _currentDir.lastIndexOf('/');
        _currentDir = idx >= 0 ? _currentDir.slice(0, idx) : '';
        refreshListing();
    });
    _el('axonos_files_newdir').addEventListener('click', async () => {
        const name = window.prompt('New folder name:');
        if (!name || !name.trim()) return;
        const res = await _apiJson('mkdir',
            { path: _joinPath(_currentDir, name.trim()) }, { method: 'POST' });
        if (!res.ok || !res.data || !res.data.ok) {
            _setStatus((res.data && res.data.error) || 'Could not create folder', true);
            return;
        }
        refreshListing();
    });
    _el('axonos_files_upload_btn').addEventListener('click', () => {
        _el('axonos_files_input').click();
    });
    _el('axonos_files_input').addEventListener('change', (ev) => {
        enqueueUploads(ev.target.files);
        ev.target.value = '';
    });

    const drop = _el('axonos_files_drop');
    drop.addEventListener('dragover', (ev) => {
        ev.preventDefault();
        drop.classList.add('axonos-files-drop--over');
    });
    drop.addEventListener('dragleave', () => {
        drop.classList.remove('axonos-files-drop--over');
    });
    drop.addEventListener('drop', (ev) => {
        ev.preventDefault();
        drop.classList.remove('axonos-files-drop--over');
        if (ev.dataTransfer && ev.dataTransfer.files) {
            enqueueUploads(ev.dataTransfer.files);
        }
    });

    // Uploads survive accidental tab close only as server-side partials;
    // warn so the user can keep the tab open until the queue drains.
    window.addEventListener('beforeunload', (ev) => {
        if (hasActiveTransfers()) {
            ev.preventDefault();
            ev.returnValue = '';
        }
    });
}

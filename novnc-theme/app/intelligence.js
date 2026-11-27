(() => {
    'use strict';
    const el = id => document.getElementById(id);
    const thread = el('thread');

    function createRenderer() {
        if (!window.markdownit || !window.mdItPluginTex || !window.katex) return null;
        const markdown = window.markdownit({html: false}).disable('image');
        markdown.validateLink = url => /^https?:\/\//i.test(url);
        markdown.use(window.mdItPluginTex.tex, {
            delimiters: 'all',
            render: (tex, displayMode) => window.katex.renderToString(tex, {
                displayMode, output: 'mathml', trust: false, throwOnError: false,
                maxSize: 10, maxExpand: 500, strict: 'ignore'
            })
        });
        return markdown;
    }
    const markdown = createRenderer();

    function renderAssistant(node, text) {
        if (markdown) {
            try {
                node.innerHTML = markdown.render(text);
                return;
            } catch { /* Preserve the original text if a renderer fails. */ }
        }
        node.textContent = text;
    }

    window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
    if (document.body.classList.contains('shared-page')) {
        for (const content of thread.querySelectorAll('.assistant .content')) {
            renderAssistant(content, content.textContent);
        }
        return;
    }

    const wallet = document.body.dataset.wallet;
    const collection = '/api/intelligence/conversations';
    const form = el('composer');
    const input = el('prompt');
    const scroll = el('chat-scroll');
    const status = el('status');
    const manage = el('manage');
    const list = el('history');
    const search = el('search');
    const title = el('chat-title');
    const shell = el('shell');
    const mobile = matchMedia('(max-width: 760px)');
    let active = true;
    let storageAvailable = null;
    let current = null;
    let busy = false;
    let listVersion = 0;
    let selectedVersion = 0;
    let searchTimer;
    let pendingTurn = null;
    let managed = null;
    let managing = false;
    let dialogVersion = 0;
    let generation = null;
    let following = true;
    let previousTop = 0;
    let scrollFrame = 0;
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

    function followLatest() {
        if (!active || !following || scrollFrame) return;
        scrollFrame = requestAnimationFrame(() => {
            scrollFrame = 0;
            if (!active || !following) return;
            if (scroll.scrollTop < previousTop - 1) {
                setFollowing(false);
                return;
            }
            const bottom = scroll.scrollHeight - scroll.clientHeight;
            const distance = bottom - scroll.scrollTop;
            if (distance <= 1) return;
            scroll.scrollTop = reducedMotion.matches || distance < 4 ? bottom : scroll.scrollTop + distance * .35;
            previousTop = scroll.scrollTop;
            if (bottom - previousTop > 1) followLatest();
        });
    }

    function setFollowing(enabled) {
        following = enabled;
        el('latest').hidden = enabled;
        previousTop = scroll.scrollTop;
    }
    scroll.addEventListener('scroll', () => {
        const top = scroll.scrollTop;
        if (top < previousTop - 1) setFollowing(false);
        if (scroll.scrollHeight - scroll.clientHeight - top <= 3) setFollowing(true);
        previousTop = top;
    }, {passive: true});
    el('latest').addEventListener('click', () => { setFollowing(true); followLatest(); });
    new ResizeObserver(followLatest).observe(scroll);

    function sameWallet() {
        try {
            return (localStorage.getItem('axonos_last_wallet') || '').toLowerCase() === wallet;
        } catch {
            return true;
        }
    }

    function clearPrivateView() {
        active = false;
        generation?.controller.abort();
        cancelAnimationFrame(scrollFrame);
        current = managed = pendingTurn = null;
        clearTimeout(searchTimer);
        thread.replaceChildren();
        list.replaceChildren();
        input.value = search.value = el('share-url').value = el('rename-title').value = '';
        title.textContent = el('dialog-title').textContent = '';
        if (manage.open) manage.close();
    }

    function signIn() {
        clearPrivateView();
        location.replace('/?view=intelligence');
    }

    async function request(path, method, data, streamSignal) {
        if (!active || !sameWallet()) {
            if (active) signIn();
            throw new Error('Wallet session ended.');
        }
        const headers = {'X-Wallet-Address': wallet};
        const options = {method, credentials: 'same-origin', cache: 'no-store', headers};
        if (streamSignal) {
            headers.Accept = 'application/x-ndjson';
            options.signal = streamSignal;
        }
        if (data !== undefined) {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(data);
            if (new TextEncoder().encode(options.body).length > 65536) {
                throw new Error('The request exceeds 64 KiB. Shorten the message or start a new chat.');
            }
        }
        const response = await fetch(path, options);
        if (response.status === 401 || !sameWallet()) {
            signIn();
            throw new Error('Wallet session ended.');
        }
        if (!response.ok) {
            const body = await response.json();
            const error = new Error(body.error || 'Intelligence could not complete the request.');
            error.status = response.status;
            throw error;
        }
        return response;
    }

    async function api(path, method = 'GET', data) {
        return (await request(path, method, data)).json();
    }

    function errorText(error) {
        return error instanceof SyntaxError || error instanceof TypeError
            ? 'Could not reach Intelligence. Try again.' : error.message;
    }

    function updateControls() {
        const waiting = busy || storageAvailable === null;
        input.readOnly = waiting;
        el('send').disabled = waiting || !input.value.trim();
        el('send').hidden = Boolean(generation);
        el('stop').hidden = !generation;
        el('stop').disabled = Boolean(generation?.stopping);
        el('reset').disabled = el('new-mobile').disabled = busy;
        el('share').hidden = !(storageAvailable && current?.id && current.messages.length);
        el('share').disabled = busy;
        for (const button of list.querySelectorAll('button')) button.disabled = busy;
        for (const button of el('starters').querySelectorAll('button')) button.disabled = waiting;
    }

    function sizeInput() {
        input.style.height = 'auto';
        input.style.height = input.scrollHeight + 'px';
    }

    function message(role, text, finishReason) {
        const entry = document.createElement('article');
        const name = document.createElement('strong');
        const content = document.createElement('div');
        entry.className = role === 'user' ? 'user' : 'assistant';
        name.textContent = role === 'user' ? 'You' : 'AxonAI';
        content.className = 'content';
        if (role === 'assistant') renderAssistant(content, text);
        else content.textContent = text;
        entry.append(name, content);
        if (role === 'assistant' && (finishReason === 'length' || finishReason === 'cancelled')) {
            const note = document.createElement('p');
            note.className = 'reply-note';
            note.textContent = finishReason === 'length' ? 'Output limit reached. Start a new chat with the last passage to continue.' : 'Generation stopped.';
            entry.append(note);
        }
        return entry;
    }

    function showConversation(conversation, resetDraft = true) {
        const readingPosition = scroll.scrollTop;
        current = conversation;
        title.textContent = conversation?.title || 'New chat';
        const messages = conversation?.messages || [];
        const last = messages.at(-1);
        if (generation && resetDraft && last?.role === 'assistant' && messages.length === thread.childElementCount) {
            // Only announce the completed answer. An idempotent replay can also
            // return newer turns, in which case render the full saved transcript.
            const completed = message(last.role, last.content, last.finish_reason);
            updateSelectedReply(generation.content, () => thread.lastElementChild.replaceWith(completed));
        } else {
            thread.replaceChildren(...messages.map(item => message(item.role, item.content, item.finish_reason)));
        }
        scroll.classList.toggle('is-empty', !thread.childElementCount);
        const url = new URL(location.href);
        if (conversation?.id) url.searchParams.set('chat', conversation.id);
        else url.searchParams.delete('chat');
        window.history.replaceState(null, '', url);
        if (resetDraft) {
            pendingTurn = null;
            input.value = status.textContent = '';
            sizeInput();
        }
        for (const row of list.children) {
            const selected = row.dataset.id === conversation?.id;
            row.classList.toggle('selected', selected);
            row.firstElementChild.setAttribute('aria-current', selected ? 'true' : 'false');
        }
        updateControls();
        if (!generation) {
            setFollowing(true);
            scroll.scrollTop = scroll.scrollHeight;
            previousTop = scroll.scrollTop;
        } else if (following) {
            followLatest();
        } else {
            scroll.scrollTop = readingPosition;
        }
    }

    function resizeSidebar(open = false) {
        const overlay = mobile.matches && open;
        shell.classList.toggle('sidebar-open', overlay);
        el('scrim').hidden = !overlay;
        el('chat-main').inert = overlay;
        if (overlay) {
            el('sidebar').setAttribute('role', 'dialog');
            el('sidebar').setAttribute('aria-modal', 'true');
            el('sidebar-close').focus();
        } else {
            el('sidebar').removeAttribute('role');
            el('sidebar').removeAttribute('aria-modal');
        }
        el('sidebar-toggle').setAttribute('aria-expanded', String(mobile.matches ? overlay : !shell.classList.contains('sidebar-hidden')));
    }

    function toggleSidebar(open) {
        if (!mobile.matches) {
            shell.classList.toggle('sidebar-hidden', !open);
            try { localStorage.setItem('axonos_intelligence_sidebar_closed', open ? '0' : '1'); } catch { /* Optional preference. */ }
        }
        resizeSidebar(open);
        if (open) el('sidebar-close').focus();
        else el('sidebar-toggle').focus();
    }
    try { shell.classList.toggle('sidebar-hidden', localStorage.getItem('axonos_intelligence_sidebar_closed') === '1'); } catch { /* Default to open. */ }
    mobile.addEventListener('change', () => resizeSidebar());
    el('sidebar-toggle').addEventListener('click', () => toggleSidebar(true));
    el('sidebar-close').addEventListener('click', () => toggleSidebar(false));
    el('scrim').addEventListener('click', () => toggleSidebar(false));
    el('sidebar').addEventListener('keydown', event => {
        if (!shell.classList.contains('sidebar-open')) return;
        if (event.key === 'Escape') toggleSidebar(false);
        if (event.key !== 'Tab') return;
        const items = [...el('sidebar').querySelectorAll('button:enabled,input,a')].filter(item => item.offsetWidth);
        const first = items[0], last = items.at(-1);
        if ((event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
            event.preventDefault();
            (event.shiftKey ? last : first).focus();
        }
    });
    resizeSidebar();

    async function selectConversation(id) {
        if (busy) return;
        const version = ++selectedVersion;
        busy = true;
        updateControls();
        try {
            const data = await api(collection + '/' + encodeURIComponent(id));
            if (!active || version !== selectedVersion) return;
            showConversation(data);
            if (mobile.matches) resizeSidebar();
            input.focus({preventScroll: true});
        } catch (error) {
            if (!active || version !== selectedVersion) return;
            if (error.status === 404) {
                if (!current || current.id === id) showConversation(null);
                void loadHistory();
            }
            status.textContent = errorText(error);
            if (mobile.matches) resizeSidebar();
        } finally {
            if (active && version === selectedVersion) {
                busy = false;
                updateControls();
            }
        }
    }

    function makeRow(item) {
        const row = document.createElement('div');
        const link = document.createElement('button');
        const options = document.createElement('button');
        row.className = 'chat-row';
        row.dataset.id = item.id;
        link.className = 'chat-link';
        link.type = options.type = 'button';
        link.textContent = link.title = item.title;
        const selected = item.id === current?.id;
        row.classList.toggle('selected', selected);
        link.setAttribute('aria-current', selected ? 'true' : 'false');
        link.addEventListener('click', () => selectConversation(item.id));
        options.className = 'chat-options icon-button';
        options.setAttribute('aria-label', 'Options for ' + item.title);
        options.setAttribute('aria-haspopup', 'dialog');
        options.innerHTML = '<svg class="icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><use href="#i-more"/></svg>';
        options.addEventListener('click', () => openManage(item, 'options'));
        row.append(link);
        if (item.shared) {
            const badge = document.createElement('span');
            badge.title = 'Shared snapshot';
            badge.innerHTML = '<svg class="icon share-indicator" aria-label="Shared snapshot" role="img" fill="none" stroke="currentColor" stroke-width="1.6" viewBox="0 0 24 24"><use href="#i-share"/></svg>';
            row.append(badge);
        }
        row.append(options);
        return row;
    }

    async function loadHistory() {
        const version = ++listVersion;
        const query = search.value.trim();
        try {
            const data = await api(collection + (query ? '?q=' + encodeURIComponent(query) : ''));
            if (!active || version !== listVersion) return;
            storageAvailable = data.storage_available !== false;
            el('temporary').hidden = storageAvailable;
            search.closest('label').hidden = !storageAvailable;
            el('retry-list').hidden = true;
            list.replaceChildren(...data.conversations.map(makeRow));
            el('list-message').textContent = !storageAvailable ? 'History requires wallet storage.'
                : query ? 'No matching conversations.' : 'Your chats will appear here.';
            el('list-message').hidden = list.childElementCount > 0;
            updateControls();
        } catch (error) {
            if (!active || version !== listVersion) return;
            el('list-message').textContent = errorText(error);
            el('list-message').hidden = el('retry-list').hidden = false;
            if (storageAvailable === null) status.textContent = 'History could not load. Retry history to start chatting.';
        }
    }
    async function initialize() {
        await loadHistory();
        if (!active || storageAvailable === null) return;
        status.textContent = '';
        const id = new URLSearchParams(location.search).get('chat');
        if (id && storageAvailable) await selectConversation(id);
        else showConversation(null);
    }
    el('retry-list').addEventListener('click', () => storageAvailable === null ? initialize() : loadHistory());
    search.addEventListener('input', () => {
        ++listVersion;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(loadHistory, 180);
    });

    function newChat() {
        if (busy) return;
        ++selectedVersion;
        showConversation(null);
        if (mobile.matches) resizeSidebar();
        input.focus();
    }
    el('reset').addEventListener('click', newChat);
    el('new-mobile').addEventListener('click', newChat);
    el('starters').addEventListener('click', event => {
        const suggestion = event.target.closest('[data-prompt]');
        if (!suggestion) return;
        input.value = suggestion.dataset.prompt;
        sizeInput();
        updateControls();
        input.focus();
    });
    input.addEventListener('input', () => { sizeInput(); updateControls(); });
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            if (!el('send').disabled) form.requestSubmit();
        }
    });

    function newRequestId() {
        if (crypto.randomUUID) return crypto.randomUUID();
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
        return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
    }

    async function* streamEvents(response) {
        if (!response.body || response.headers.get('Content-Type')?.split(';')[0] !== 'application/x-ndjson') {
            throw new Error('Intelligence returned an unreadable response. Try again.');
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8', {fatal: true});
        // A saved completion carries both the transcript (up to 8 MiB on
        // the wire) and a second JSON-escaped copy of the final reply.
        const maxFrameBytes = 16 * 1024 * 1024;
        let pieces = [];
        let bytes = 0;

        function readPiece(piece, endOfFrame) {
            bytes += piece.byteLength;
            if (bytes > maxFrameBytes) throw new Error('The response frame is too large to read.');
            const decoded = decoder.decode(piece, {stream: !endOfFrame});
            if (decoded) pieces.push(decoded);
            if (!endOfFrame) return;
            const line = pieces.join('');
            pieces = [];
            bytes = 0;
            if (line.trim()) return JSON.parse(line);
        }
        try {
            while (true) {
                const {done, value = new Uint8Array()} = await reader.read();
                let start = 0;
                for (let end = value.indexOf(10); end !== -1; end = value.indexOf(10, start)) {
                    const event = readPiece(value.subarray(start, end), true);
                    if (event) yield event;
                    start = end + 1;
                }
                const event = readPiece(value.subarray(start), done);
                if (event) yield event;
                if (done) break;
            }
        } finally {
            await reader.cancel().catch(() => {});
            reader.releaseLock();
        }
    }

    async function stopRemote(turn) {
        try {
            await api('/api/intelligence/generations/' + turn.id + '/stop', 'POST', {});
        } catch (error) {
            if (!active || generation !== turn) return;
            turn.stopping = false;
            updateControls();
            status.textContent = 'Could not confirm the stop. ' + errorText(error);
        }
    }
    el('stop').addEventListener('click', () => {
        if (!generation || generation.stopping) return;
        generation.stopping = true;
        status.textContent = 'Stopping generation…';
        updateControls();
        if (generation.started) void stopRemote(generation);
    });

    function readingSelection(node) {
        const selection = document.getSelection();
        if (following || !selection?.rangeCount || selection.isCollapsed) return null;
        const range = selection.getRangeAt(0);
        return thread.contains(range.commonAncestorContainer) && range.intersectsNode(node) ? range : null;
    }

    function updateSelectedReply(node, update) {
        const range = readingSelection(node);
        if (!range) return update();
        const prefix = range.cloneRange();
        prefix.selectNodeContents(thread);
        prefix.setEnd(range.startContainer, range.startOffset);
        const offset = prefix.toString().length;
        const quote = range.toString();
        update();

        // The finalized Markdown can change the DOM around an existing
        // selection. Restore its text at the closest occurrence in the result.
        const text = thread.textContent;
        const left = text.lastIndexOf(quote, offset);
        const right = text.indexOf(quote, offset);
        const start = right === -1 || (left >= 0 && offset - left <= right - offset) ? left : right;
        if (!quote || start === -1) return;
        const walker = document.createTreeWalker(thread, NodeFilter.SHOW_TEXT);
        const restored = document.createRange();
        let index = 0;
        for (let n = walker.nextNode(); n; n = walker.nextNode()) {
            const end = index + n.length;
            if (index <= start && start < end) restored.setStart(n, start - index);
            if (end >= start + quote.length) {
                restored.setEnd(n, start + quote.length - index);
                const selection = document.getSelection();
                selection.removeAllRanges();
                selection.addRange(restored);
                return;
            }
            index = end;
        }
    }

    function paintReply(turn) {
        if (!active || generation !== turn) return;
        turn.paint = 0;
        if (readingSelection(turn.content)) return;
        const start = performance.now();
        renderAssistant(turn.content, turn.text);
        const completed = performance.now();
        // Coalesce more deltas as an answer grows or Markdown/math layout
        // gets expensive. The first text and the final answer render immediately.
        const interval = Math.max(85, turn.text.length / 32, (completed - start) * 14);
        turn.nextPaint = completed + Math.min(1000, interval);
        followLatest();
    }

    async function generateReply(question, turn) {
        let path, payload;
        const saved = storageAvailable;
        const messages = current?.messages || [];
        if (saved) {
            if (!current) {
                const created = await api(collection, 'POST', {});
                if (!active) return;
                current = created;
                const url = new URL(location.href);
                url.searchParams.set('chat', created.id);
                window.history.replaceState(null, '', url);
                void loadHistory();
            }
            if (!pendingTurn || pendingTurn.prompt !== question || pendingTurn.revision !== current.revision) {
                pendingTurn = {prompt: question, revision: current.revision, request_id: turn.id};
            }
            turn.id = pendingTurn.request_id;
            path = collection + '/' + current.id + '/messages';
            payload = pendingTurn;
        } else {
            const lines = messages.map(item => (item.role === 'user' ? 'User: ' : 'AxonAI: ') + item.content);
            const prompt = lines.length ? [...lines, 'User: ' + question].join('\n\n') : question;
            path = '/api/intelligence/generate';
            payload = {prompt, request_id: turn.id};
        }
        const response = await request(path, 'POST', payload, turn.controller.signal);
        for await (const event of streamEvents(response)) {
            if (!active || !sameWallet()) {
                if (active) signIn();
                return;
            }
            if (event.type === 'start') {
                turn.started = true;
                if (turn.stopping) void stopRemote(turn);
            } else if (event.type === 'delta' && typeof event.text === 'string' && event.text) {
                turn.text += event.text;
                const delay = turn.nextPaint - performance.now();
                if (!turn.paint && delay <= 0) paintReply(turn);
                else if (!turn.paint) turn.paint = setTimeout(() => paintReply(turn), delay);
            } else if (event.type === 'error') {
                if (event.status === 401) {
                    signIn();
                    return;
                }
                const error = new Error(event.error || 'Generation was interrupted.');
                error.status = event.status;
                throw error;
            } else if (event.type === 'done') {
                if (!['stop', 'length', 'cancelled'].includes(event.finish_reason)
                    || typeof event.text !== 'string' || (saved && !Array.isArray(event.conversation?.messages))) {
                    throw new Error('The response ended without a valid completion record.');
                }
                if (saved) return event.conversation;
                return {title: current?.title || question, messages: [...messages, {role: 'user', content: question},
                    {role: 'assistant', content: event.text, finish_reason: event.finish_reason}]};
            }
        }
        throw new Error('The stream ended before completion.');
    }

    form.addEventListener('submit', async event => {
        event.preventDefault();
        const question = input.value.trim();
        if (busy || !question || storageAvailable === null) return;
        if (new TextEncoder().encode(question).length > 65536) {
            status.textContent = 'The message exceeds 64 KiB. Shorten it before sending.';
            return;
        }
        thread.querySelectorAll('.interrupted-turn').forEach(entry => entry.remove());
        const user = message('user', question);
        const assistant = message('assistant', '');
        const content = assistant.querySelector('.content');
        content.setAttribute('aria-live', 'off');
        content.append(el('waiting-indicator').content.cloneNode(true));
        const turn = {id: newRequestId(), content, text: '', started: false, stopping: false, paint: 0, nextPaint: 0, controller: new AbortController()};
        generation = turn;
        busy = true;
        updateControls();
        thread.append(user, assistant);
        scroll.classList.remove('is-empty');
        setFollowing(true);
        followLatest();
        input.value = status.textContent = '';
        sizeInput();
        try {
            const conversation = await generateReply(question, turn);
            if (!active) return;
            showConversation(conversation);
            if (storageAvailable) void loadHistory();
        } catch (error) {
            if (!active) return;
            if (error.status === 409 && current?.id) {
                try { current = await api(collection + '/' + current.id); } catch { /* Keep the draft if reload fails. */ }
                pendingTurn = null;
            }
            if (!active) return;
            if (turn.text && error.status !== 404 && error.status !== 409) {
                updateSelectedReply(turn.content, () => renderAssistant(turn.content, turn.text));
                user.classList.add('interrupted-turn');
                assistant.classList.add('interrupted-turn');
                const note = document.createElement('p');
                note.className = 'reply-note';
                note.textContent = 'Stream interrupted. This reply may be incomplete.';
                assistant.append(note);
                followLatest();
            } else {
                showConversation(error.status === 404 ? null : current, false);
            }
            input.value = question;
            sizeInput();
            status.textContent = errorText(error);
        } finally {
            clearTimeout(turn.paint);
            generation = null;
            if (active) {
                busy = false;
                updateControls();
                if (!mobile.matches && (document.activeElement === document.body || form.contains(document.activeElement))) {
                    input.focus({preventScroll: true});
                }
            }
        }
    });

    function setShare(url) {
        const exists = Boolean(url);
        el('share-description').textContent = exists
            ? 'Anyone with this link can read the saved snapshot. Later messages stay private.'
            : 'Create a read-only snapshot of the current title and messages. Anyone with the link can read it until you revoke access.';
        el('share-url').value = url ? new URL(url, location.origin).href : '';
        el('share-url-label').hidden = el('copy-link').hidden = el('revoke-link').hidden = !exists;
        el('create-link').hidden = exists;
    }
    function setPanel(mode) {
        for (const key of ['options', 'rename', 'delete', 'share']) el('dialog-' + key).hidden = key !== mode;
        const names = {options: managed.title, rename: 'Rename conversation', delete: 'Delete conversation?', share: 'Share conversation'};
        el('dialog-title').textContent = names[mode];
        el('dialog-status').textContent = '';
        if (mode === 'rename') {
            el('rename-title').value = managed.title;
            el('rename-title').focus();
            el('rename-title').select();
        }
    }
    async function openManage(item, mode) {
        if (busy || managing) return;
        managed = item;
        if (!manage.open) manage.showModal();
        setPanel(mode);
        if (mode !== 'share') return;
        const version = ++dialogVersion;
        const id = item.id;
        el('dialog-share').hidden = true;
        el('dialog-status').textContent = 'Loading sharing settings…';
        try {
            const data = await api(collection + '/' + id);
            if (!active || !manage.open || version !== dialogVersion) return;
            managed = data;
            setShare(data.share_url);
            el('dialog-share').hidden = false;
            el('create-link').disabled = !data.messages.length;
            el('dialog-status').textContent = data.messages.length ? '' : 'Send a message before sharing this conversation.';
        } catch (error) {
            if (active && version === dialogVersion) el('dialog-status').textContent = errorText(error);
        }
    }
    manage.addEventListener('close', () => {
        ++dialogVersion;
        managed = null;
        el('share-url').value = '';
    });
    for (const button of document.querySelectorAll('.close-dialog')) {
        button.addEventListener('click', () => button.closest('dialog').close());
    }
    el('model-info').addEventListener('click', () => el('about').showModal());
    el('share').addEventListener('click', () => openManage(current, 'share'));
    el('dialog-options').addEventListener('click', event => {
        const mode = event.target.closest('[data-action]')?.dataset.action;
        if (mode) void openManage(managed, mode);
    });

    async function modifyConversation(action) {
        if (managing || !managed) return;
        const id = managed.id;
        const path = collection + '/' + id;
        const version = dialogVersion;
        managing = true;
        for (const button of manage.querySelectorAll('button')) button.disabled = true;
        el('dialog-status').textContent = 'Saving…';
        try {
            if (action === 'delete') {
                await api(path, 'DELETE');
                if (!active) return;
                if (current?.id === id) showConversation(null);
                manage.close();
            } else if (action === 'rename') {
                const updated = await api(path, 'PATCH', {title: el('rename-title').value.trim()});
                if (!active) return;
                if (current?.id === id) showConversation(updated, false);
                manage.close();
            } else {
                const creating = action === 'create';
                const result = await api(path + '/share', creating ? 'POST' : 'DELETE', creating ? {} : undefined);
                if (!active || version !== dialogVersion) return;
                setShare(creating ? result.url : null);
                el('dialog-status').textContent = creating ? 'Share link created.' : 'Share link revoked.';
            }
            if (active) void loadHistory();
        } catch (error) {
            if (active && version === dialogVersion) el('dialog-status').textContent = errorText(error);
        } finally {
            managing = false;
            for (const button of manage.querySelectorAll('button')) button.disabled = false;
        }
    }
    manage.addEventListener('cancel', event => { if (managing) event.preventDefault(); });
    el('dialog-rename').addEventListener('submit', event => {
        event.preventDefault();
        if (el('rename-title').value.trim()) void modifyConversation('rename');
    });
    el('confirm-delete').addEventListener('click', () => modifyConversation('delete'));
    el('create-link').addEventListener('click', () => modifyConversation('create'));
    el('revoke-link').addEventListener('click', () => modifyConversation('revoke'));
    el('copy-link').addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(el('share-url').value);
            el('dialog-status').textContent = 'Link copied.';
        } catch {
            el('share-url').select();
            el('dialog-status').textContent = 'Select Copy to copy the highlighted link.';
        }
    });

    // Keep the existing workspace token rotation active on a visible signed-in page.
    async function refreshAuth() {
        if (!active || document.hidden) return;
        if (!sameWallet()) return signIn();
        try {
            const response = await fetch('/api/auth/wallet-status', {
                headers: {'X-Wallet-Address': wallet}, credentials: 'same-origin', cache: 'no-store'
            });
            if (active && response.status === 401) signIn();
        } catch { /* Retry on the next tick. */ }
    }
    window.addEventListener('storage', () => { if (!sameWallet()) signIn(); });
    window.addEventListener('pagehide', clearPrivateView);
    document.addEventListener('visibilitychange', refreshAuth);
    setInterval(refreshAuth, 60000);
    updateControls();
    if (!sameWallet()) signIn();
    else void initialize();
})();

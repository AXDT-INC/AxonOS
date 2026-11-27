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

    function sameWallet() {
        try {
            return (localStorage.getItem('axonos_last_wallet') || '').toLowerCase() === wallet;
        } catch {
            return true;
        }
    }

    function clearPrivateView() {
        active = false;
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

    async function api(path, method = 'GET', data) {
        if (!active || !sameWallet()) {
            if (active) signIn();
            throw new Error('Wallet session ended.');
        }
        const headers = {'X-Wallet-Address': wallet};
        const options = {method, credentials: 'same-origin', cache: 'no-store', headers};
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
        const body = await response.json();
        if (!response.ok) {
            const error = new Error(body.error || 'Intelligence could not complete the request.');
            error.status = response.status;
            throw error;
        }
        return body;
    }

    function errorText(error) {
        return error instanceof SyntaxError || error instanceof TypeError
            ? 'Could not reach Intelligence. Try again.' : error.message;
    }

    function updateControls() {
        const waiting = busy || storageAvailable === null;
        input.readOnly = waiting;
        el('send').disabled = waiting || !input.value.trim();
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

    function message(role, text) {
        const entry = document.createElement('article');
        const name = document.createElement('strong');
        const content = document.createElement('div');
        entry.className = role === 'user' ? 'user' : 'assistant';
        name.textContent = role === 'user' ? 'You' : 'AxonAI';
        content.className = 'content';
        if (role === 'assistant') renderAssistant(content, text);
        else content.textContent = text;
        entry.append(name, content);
        return entry;
    }

    function showConversation(conversation, resetDraft = true) {
        current = conversation;
        title.textContent = conversation?.title || 'New chat';
        thread.replaceChildren(...(conversation?.messages || []).map(item => message(item.role, item.content)));
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
        scroll.scrollTop = scroll.scrollHeight;
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

    async function saveTurn(question) {
        if (!storageAvailable) {
            const messages = current?.messages || [];
            const lines = messages.map(item => (item.role === 'user' ? 'User: ' : 'AxonAI: ') + item.content);
            const prompt = lines.length ? [...lines, 'User: ' + question].join('\n\n') : question;
            const answer = await api('/api/intelligence/generate', 'POST', {prompt});
            return {title: current?.title || question, messages: [...messages, {role: 'user', content: question}, {role: 'assistant', content: answer.text}]};
        }
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
            pendingTurn = {prompt: question, revision: current.revision, request_id: newRequestId()};
        }
        return api(collection + '/' + current.id + '/messages', 'POST', pendingTurn);
    }

    form.addEventListener('submit', async event => {
        event.preventDefault();
        const question = input.value.trim();
        if (busy || !question || storageAvailable === null) return;
        if (new TextEncoder().encode(question).length > 65536) {
            status.textContent = 'The message exceeds 64 KiB. Shorten it before sending.';
            return;
        }
        busy = true;
        updateControls();
        thread.append(message('user', question));
        scroll.classList.remove('is-empty');
        scroll.scrollTop = scroll.scrollHeight;
        input.value = '';
        sizeInput();
        status.textContent = 'Waiting for AxonAI…';
        try {
            const conversation = await saveTurn(question);
            if (!active) return;
            showConversation(conversation);
            if (storageAvailable) void loadHistory();
        } catch (error) {
            if (!active) return;
            if (error.status === 409 && current?.id) {
                try { current = await api(collection + '/' + current.id); } catch { /* Preserve the draft on a failed reload. */ }
                pendingTurn = null;
            }
            if (!active) return;
            showConversation(error.status === 404 ? null : current, false);
            input.value = question;
            sizeInput();
            status.textContent = errorText(error);
        } finally {
            if (active) {
                busy = false;
                updateControls();
                input.focus({preventScroll: true});
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

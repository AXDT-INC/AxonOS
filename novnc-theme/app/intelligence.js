(() => {
    'use strict';
    const wallet = document.body.dataset.wallet;
    const form = document.querySelector('form');
    const input = document.getElementById('prompt');
    const thread = document.getElementById('thread');
    const status = document.getElementById('status');
    const send = document.getElementById('send');
    const reset = document.getElementById('reset');
    const turns = [];
    let active = true;

    function clearChat() {
        turns.length = 0;
        thread.replaceChildren();
        input.value = '';
        status.textContent = '';
    }

    function signIn() {
        active = false;
        clearChat();
        location.replace('/?view=intelligence');
    }

    function sameWallet() {
        try {
            return (localStorage.getItem('axonos_last_wallet') || '').toLowerCase() === wallet;
        } catch {
            // The server also checks the cookie against the page wallet on every send.
            return true;
        }
    }

    function message(author, text) {
        const entry = document.createElement('article');
        const name = document.createElement('strong');
        const content = document.createElement('p');
        name.textContent = author;
        content.textContent = text;
        entry.append(name, content);
        thread.append(entry);
        return entry;
    }

    reset.addEventListener('click', () => {
        clearChat();
        input.focus();
    });
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            if (!send.disabled) form.requestSubmit();
        }
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (!active || !sameWallet()) return signIn();
        const question = input.value.trim();
        if (!question || send.disabled) return;
        const prompt = turns.length ? [...turns, 'User: ' + question].join('\n\n') : question;
        const body = JSON.stringify({prompt});
        if (new TextEncoder().encode(body).length > 65536) {
            status.textContent = 'The conversation exceeds 64 KiB. Shorten it or start a new chat.';
            return;
        }
        send.disabled = reset.disabled = input.readOnly = true;
        const entry = message('You', question);
        input.value = '';
        status.textContent = 'Waiting for AxonAI…';
        try {
            const response = await fetch('/api/intelligence/generate', {
                method: 'POST', credentials: 'same-origin', cache: 'no-store',
                headers: {'Content-Type': 'application/json', 'X-Wallet-Address': wallet}, body
            });
            if (response.status === 401 || !sameWallet()) return signIn();
            const reply = await response.json();
            if (!active) return;
            if (!response.ok) throw new Error(reply.error || 'AxonAI could not generate a reply.');
            turns.push('User: ' + question, 'AxonAI: ' + reply.text);
            message('AxonAI', reply.text || '(No text returned.)');
            status.textContent = '';
        } catch (error) {
            if (!active) return;
            entry.remove();
            input.value = question;
            status.textContent = error instanceof SyntaxError || error instanceof TypeError
                ? 'Could not reach Intelligence. Try again.' : error.message;
        } finally {
            if (active) {
                send.disabled = reset.disabled = input.readOnly = false;
                input.focus();
            }
        }
    });

    // Use the workspace's existing token rotation while this signed-in page is visible.
    async function refreshAuth() {
        if (!active || document.hidden) return;
        if (!sameWallet()) return signIn();
        try {
            const response = await fetch('/api/auth/wallet-status', {
                headers: {'X-Wallet-Address': wallet}, credentials: 'same-origin', cache: 'no-store'
            });
            if (active && response.status === 401) signIn();
        } catch { /* A transient transport failure can be retried on the next tick. */ }
    }
    window.addEventListener('storage', () => { if (!sameWallet()) signIn(); });
    window.addEventListener('pagehide', () => { active = false; clearChat(); });
    window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
    document.addEventListener('visibilitychange', refreshAuth);
    setInterval(refreshAuth, 60000);
    if (!sameWallet()) signIn();
})();

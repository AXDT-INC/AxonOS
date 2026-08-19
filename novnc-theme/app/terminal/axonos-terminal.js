/*
 * AxonOS browser-terminal transport.
 *
 * The terminal emulator is the pinned, self-hosted xterm.js build under
 * app/vendor/.  This module owns only the authenticated ticket exchange,
 * framed WebSocket protocol, renderer lifecycle, resize, and focus handling.
 */

import { Terminal } from '../vendor/xterm/xterm.mjs';
import { FitAddon } from '../vendor/xterm/addon-fit.mjs';

const FRAME_HEADER_BYTES = 5;
const MAX_INPUT_BYTES = 65536;
const MAX_RESIZE_BYTES = 1024;
const MAX_SERVER_PAYLOAD_BYTES = 4 * 1024 * 1024;
const MAX_RECEIVE_BUFFER_BYTES = 16 * 1024 * 1024;
const MAX_SOCKET_BUFFERED_BYTES = 1024 * 1024;
const CONNECT_TIMEOUT_MS = 10000;
const TICKET_TIMEOUT_MS = 10000;
const PING_INTERVAL_MS = 20000;
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: false });

function terminalError(message, code) {
    const error = new Error(message);
    error.code = code;
    return error;
}

function jsonPayload(value) {
    return encoder.encode(JSON.stringify(value));
}

function encodeFrame(type, payload) {
    const body = payload instanceof Uint8Array
        ? payload
        : (payload ? new Uint8Array(payload) : new Uint8Array(0));
    const frame = new Uint8Array(FRAME_HEADER_BYTES + body.byteLength);
    frame[0] = type.charCodeAt(0);
    new DataView(frame.buffer).setUint32(1, body.byteLength, false);
    frame.set(body, FRAME_HEADER_BYTES);
    return frame;
}

function decodeFrame(data) {
    const bytes = data instanceof Uint8Array
        ? data
        : (data instanceof ArrayBuffer ? new Uint8Array(data) : null);
    if (!bytes) {
        throw terminalError('Terminal server sent a non-binary message.', 'protocol_error');
    }
    if (bytes.byteLength < FRAME_HEADER_BYTES) {
        throw terminalError('Terminal server sent a truncated frame.', 'protocol_error');
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const payloadLength = view.getUint32(1, false);
    if (payloadLength > MAX_SERVER_PAYLOAD_BYTES ||
        bytes.byteLength !== FRAME_HEADER_BYTES + payloadLength) {
        throw terminalError('Terminal server sent an invalid frame length.', 'protocol_error');
    }
    return {
        type: String.fromCharCode(view.getUint8(0)),
        payload: bytes.subarray(FRAME_HEADER_BYTES),
    };
}

function parseJsonFrame(payload, fallback) {
    try {
        return JSON.parse(decoder.decode(payload));
    } catch (error) {
        return fallback;
    }
}

function boundedDimension(value, fallback, minimum = 1) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(minimum, Math.min(1000, Math.floor(parsed)));
}

function authenticatedHeaders(wallet, authToken) {
    const headers = {
        'Content-Type': 'application/json',
        'X-Wallet-Address': wallet,
    };
    if (authToken) {
        headers['X-AXGT-Auth-Token'] = authToken;
    }
    return headers;
}

async function requestTerminalTicket(wallet, authToken, externalSignal) {
    const controller = typeof AbortController !== 'undefined'
        ? new AbortController()
        : null;
    const abortRequest = () => {
        if (controller) controller.abort();
    };
    if (externalSignal && externalSignal.aborted) abortRequest();
    if (externalSignal && controller) {
        externalSignal.addEventListener('abort', abortRequest, { once: true });
    }
    const timeout = setTimeout(() => {
        if (controller) controller.abort();
    }, TICKET_TIMEOUT_MS);
    try {
        const response = await fetch(
            new URL('/api/terminal/ticket', window.location.origin).toString(),
            {
                method: 'POST',
                credentials: 'include',
                cache: 'no-store',
                headers: authenticatedHeaders(wallet, authToken),
                body: JSON.stringify({ wallet_address: wallet }),
                ...((controller || externalSignal)
                    ? { signal: controller ? controller.signal : externalSignal }
                    : {}),
            }
        );
        let data = {};
        try {
            data = await response.json();
        } catch (error) {
            throw terminalError(
                `Terminal ticket returned HTTP ${response.status} without JSON.`,
                'ticket_failed'
            );
        }
        if (!response.ok || data.ok !== true || !data.ticket || !data.websocket_path) {
            throw terminalError(
                String(data.error || data.reason || 'The web terminal is unavailable.'),
                String(data.code || data.error_code || 'ticket_failed')
            );
        }
        return data;
    } catch (error) {
        if (error && error.name === 'AbortError') {
            if (externalSignal && externalSignal.aborted) {
                throw terminalError('Terminal connection was cancelled.', 'cancelled');
            }
            throw terminalError('Terminal ticket request timed out.', 'ticket_timeout');
        }
        throw error;
    } finally {
        clearTimeout(timeout);
        if (externalSignal && controller) {
            externalSignal.removeEventListener('abort', abortRequest);
        }
    }
}

function terminalWebSocketUrl(ticketResponse) {
    const url = new URL(ticketResponse.websocket_path, window.location.origin);
    if (url.protocol === 'http:') url.protocol = 'ws:';
    if (url.protocol === 'https:') url.protocol = 'wss:';
    const expectedProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    if (url.host !== window.location.host || url.protocol !== expectedProtocol) {
        throw terminalError('Rejected a cross-origin terminal endpoint.', 'unsafe_endpoint');
    }
    const keys = Array.from(url.searchParams.keys());
    if (keys.length !== 1 || keys[0] !== 'ticket' ||
        url.searchParams.get('ticket') !== String(ticketResponse.ticket)) {
        throw terminalError('Terminal endpoint did not contain the issued ticket.', 'unsafe_endpoint');
    }
    return url.toString();
}

class AxonosXtermRenderer {
    constructor(container) {
        if (!container) {
            throw terminalError('Terminal viewer container is missing.', 'renderer_unavailable');
        }
        this.container = container;
        this.mount = document.createElement('div');
        this.mount.id = 'axonos_terminal_viewer';
        this.mount.className = 'axonos-terminal-viewer';
        this.mount.tabIndex = 0;
        this.mount.setAttribute('role', 'application');
        this.mount.setAttribute('aria-label', 'AxonOS SSH web terminal');
        this.container.appendChild(this.mount);
        this.container.classList.add('axonos-terminal-active');

        this.terminal = new Terminal({
            allowProposedApi: false,
            allowTransparency: false,
            convertEol: false,
            cursorBlink: true,
            cursorStyle: 'block',
            fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace',
            fontSize: 14,
            lineHeight: 1.16,
            scrollback: 10000,
            theme: {
                background: '#080910',
                foreground: '#e8e9f2',
                cursor: '#7b6cff',
                cursorAccent: '#080910',
                selectionBackground: '#4ec3d455',
                black: '#080910',
                brightBlack: '#5d6070',
                red: '#ff6b7a',
                brightRed: '#ff8793',
                green: '#6fd89a',
                brightGreen: '#8ce6ad',
                yellow: '#f2c14e',
                brightYellow: '#f7d578',
                blue: '#7b6cff',
                brightBlue: '#9b90ff',
                magenta: '#c17cff',
                brightMagenta: '#d29aff',
                cyan: '#4ec3d4',
                brightCyan: '#75d6e2',
                white: '#d8dae5',
                brightWhite: '#ffffff',
            },
        });
        this.fitAddon = new FitAddon();
        this.terminal.loadAddon(this.fitAddon);
        this.terminal.open(this.mount);

        this._fitQueued = false;
        this._resizeObserver = typeof ResizeObserver !== 'undefined'
            ? new ResizeObserver(() => this.fit())
            : null;
        if (this._resizeObserver) {
            this._resizeObserver.observe(this.container);
        }
        this._windowResize = () => this.fit();
        window.addEventListener('resize', this._windowResize);
        this.fit();
    }

    fit() {
        if (this._fitQueued || !this.terminal) return;
        this._fitQueued = true;
        requestAnimationFrame(() => {
            this._fitQueued = false;
            if (!this.terminal || !this.mount.isConnected) return;
            try {
                this.fitAddon.fit();
            } catch (error) {
                // A hidden/transitioning container can briefly have no measurable cells.
            }
        });
    }

    focus() {
        if (this.terminal) this.terminal.focus();
    }

    write(data) {
        if (this.terminal) this.terminal.write(data);
    }

    onData(handler) {
        return this.terminal.onData(handler);
    }

    onResize(handler) {
        return this.terminal.onResize(handler);
    }

    dimensions() {
        return {
            cols: boundedDimension(this.terminal && this.terminal.cols, 80, 2),
            rows: boundedDimension(this.terminal && this.terminal.rows, 24),
        };
    }

    dispose() {
        if (this._resizeObserver) this._resizeObserver.disconnect();
        window.removeEventListener('resize', this._windowResize);
        if (this.terminal) this.terminal.dispose();
        this.terminal = null;
        if (this.mount && this.mount.parentNode) this.mount.parentNode.removeChild(this.mount);
        if (this.container) this.container.classList.remove('axonos-terminal-active');
    }
}

export class AxonosTerminalClient {
    constructor(options) {
        this.options = options || {};
        this.socket = null;
        this.renderer = null;
        this.connected = false;
        this.intentionalClose = false;
        this.disposed = false;
        this.pingId = null;
        this.inputSubscription = null;
        this.resizeSubscription = null;
        this.receiveBuffer = new Uint8Array(0);
        this.exitDetail = null;
        this.lastError = null;
    }

    _send(type, payload) {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
        this.socket.send(encodeFrame(type, payload));
        return true;
    }

    _sendResize(size) {
        const payload = jsonPayload({
            cols: boundedDimension(size && size.cols, 80, 2),
            rows: boundedDimension(size && size.rows, 24),
        });
        if (payload.byteLength <= MAX_RESIZE_BYTES) this._send('R', payload);
    }

    _sendInput(text) {
        const bytes = encoder.encode(String(text || ''));
        for (let offset = 0; offset < bytes.byteLength; offset += MAX_INPUT_BYTES) {
            if (!this.socket || this.socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) {
                const error = terminalError(
                    'Terminal input transport is congested; the connection was closed safely.',
                    'input_backpressure'
                );
                this.lastError = error;
                if (typeof this.options.onError === 'function') this.options.onError(error);
                if (this.socket && this.socket.readyState < WebSocket.CLOSING) {
                    this.socket.close(4013, 'terminal input congested');
                }
                return;
            }
            this._send('I', bytes.subarray(offset, offset + MAX_INPUT_BYTES));
        }
    }

    _dispatchServerFrame(frame) {
        if (frame.type === 'O') {
            // A queued message can race an intentional Detach/End with renderer
            // disposal. Ignore that tail instead of turning a clean close into a
            // spurious protocol error.
            if (this.renderer) this.renderer.write(frame.payload);
            return;
        }
        if (frame.type === 'X') {
            const detail = parseJsonFrame(frame.payload, { code: null });
            this.exitDetail = detail;
            // A final process-exit frame can already be queued when the user
            // intentionally detaches or ends the session. Do not let that
            // stale frame replace the intentional-close status in the UI.
            if (!this.disposed && !this.intentionalClose &&
                typeof this.options.onExit === 'function') {
                this.options.onExit(detail);
            }
            return;
        }
        if (frame.type === 'E') {
            const detail = parseJsonFrame(frame.payload, { error: 'Terminal server error.' });
            const error = terminalError(
                String(detail.error || 'Terminal server error.'),
                'server_error'
            );
            this.lastError = error;
            if (typeof this.options.onError === 'function') this.options.onError(error);
            if (this.socket && this.socket.readyState < WebSocket.CLOSING) {
                this.socket.close(4011, 'terminal server error');
            }
            return;
        }
        throw terminalError('Terminal server sent an unknown frame type.', 'protocol_error');
    }

    _handleServerData(data) {
        if (!(data instanceof ArrayBuffer)) {
            const error = terminalError(
                'Terminal server sent a non-binary message.',
                'protocol_error'
            );
            this.lastError = error;
            if (typeof this.options.onError === 'function') this.options.onError(error);
            this._protocolClose();
            return;
        }
        const incoming = new Uint8Array(data);
        if (this.receiveBuffer.byteLength + incoming.byteLength > MAX_RECEIVE_BUFFER_BYTES) {
            const error = terminalError('Terminal receive buffer limit exceeded.', 'protocol_error');
            this.lastError = error;
            if (typeof this.options.onError === 'function') this.options.onError(error);
            this._protocolClose();
            return;
        }
        let buffer;
        if (this.receiveBuffer.byteLength === 0) {
            buffer = incoming;
        } else {
            buffer = new Uint8Array(this.receiveBuffer.byteLength + incoming.byteLength);
            buffer.set(this.receiveBuffer, 0);
            buffer.set(incoming, this.receiveBuffer.byteLength);
        }

        let offset = 0;
        try {
            while (buffer.byteLength - offset >= FRAME_HEADER_BYTES) {
                const header = new DataView(
                    buffer.buffer,
                    buffer.byteOffset + offset,
                    FRAME_HEADER_BYTES
                );
                const payloadLength = header.getUint32(1, false);
                if (payloadLength > MAX_SERVER_PAYLOAD_BYTES) {
                    throw terminalError('Terminal server frame is too large.', 'protocol_error');
                }
                const frameLength = FRAME_HEADER_BYTES + payloadLength;
                if (buffer.byteLength - offset < frameLength) break;
                this._dispatchServerFrame(decodeFrame(buffer.subarray(offset, offset + frameLength)));
                offset += frameLength;
            }
        } catch (error) {
            this.lastError = error;
            if (typeof this.options.onError === 'function') this.options.onError(error);
            this.receiveBuffer = new Uint8Array(0);
            this._protocolClose();
            return;
        }
        this.receiveBuffer = offset === buffer.byteLength
            ? new Uint8Array(0)
            : buffer.slice(offset);
    }

    _protocolClose() {
        if (this.socket && this.socket.readyState < WebSocket.CLOSING) {
            this.socket.close(4002, 'terminal protocol error');
        }
    }

    async connect() {
        const wallet = String(this.options.wallet || '').trim();
        if (!wallet) throw terminalError('Wallet verification is required.', 'wallet_required');
        const signal = this.options.signal || null;
        if (signal && signal.aborted) {
            throw terminalError('Terminal connection was cancelled.', 'cancelled');
        }
        const ticket = await requestTerminalTicket(
            wallet,
            this.options.authToken || null,
            signal
        );
        this.options.authToken = null;
        if (this.disposed || (signal && signal.aborted)) {
            throw terminalError('Terminal connection was cancelled.', 'cancelled');
        }

        this.renderer = new AxonosXtermRenderer(this.options.container);
        const url = terminalWebSocketUrl(ticket);

        return new Promise((resolve, reject) => {
            let settled = false;
            const socket = new WebSocket(url);
            this.socket = socket;
            socket.binaryType = 'arraybuffer';
            const cancelConnection = () => {
                this.intentionalClose = true;
                this.disposed = true;
                if (!settled) {
                    settled = true;
                    clearTimeout(timeout);
                    reject(terminalError('Terminal connection was cancelled.', 'cancelled'));
                }
                if (socket.readyState < WebSocket.CLOSING) {
                    socket.close(1000, 'cancelled');
                }
                this.connected = false;
                this._disposeRenderer();
            };
            if (signal) signal.addEventListener('abort', cancelConnection, { once: true });
            const timeout = setTimeout(() => {
                if (settled) return;
                settled = true;
                socket.close(1000, 'connect timeout');
                reject(terminalError('Terminal connection timed out.', 'connect_timeout'));
            }, CONNECT_TIMEOUT_MS);

            socket.addEventListener('open', () => {
                if (settled || this.disposed) {
                    socket.close(1000, 'cancelled');
                    return;
                }
                settled = true;
                clearTimeout(timeout);
                this.connected = true;
                this.inputSubscription = this.renderer.onData((data) => this._sendInput(data));
                this.resizeSubscription = this.renderer.onResize((size) => this._sendResize(size));
                this._sendResize(this.renderer.dimensions());
                this.pingId = setInterval(() => this._send('P'), PING_INTERVAL_MS);
                this.renderer.focus();
                resolve(this);
            });
            socket.addEventListener('message', (event) => this._handleServerData(event.data));
            socket.addEventListener('error', () => {
                // Browsers intentionally make WebSocket error events opaque, but
                // normally follow them with a close event carrying the useful
                // server reason/code. Preserve the error and let close reject so
                // fallback can display that actionable detail instead of racing it.
                if (!this.lastError) {
                    this.lastError = terminalError(
                        'Could not open the terminal WebSocket.',
                        'socket_failed'
                    );
                }
            });
            socket.addEventListener('close', (event) => {
                clearTimeout(timeout);
                if (signal) signal.removeEventListener('abort', cancelConnection);
                const wasConnected = this.connected;
                this.connected = false;
                this._disposeRenderer();
                if (!settled) {
                    settled = true;
                    const closeReason = String(event.reason || '').trim();
                    const closeError = terminalError(
                        closeReason || (this.lastError && this.lastError.message) ||
                            'The terminal WebSocket closed before connecting.',
                        closeReason ? 'socket_closed' :
                            ((this.lastError && this.lastError.code) || 'socket_closed')
                    );
                    this.lastError = closeError;
                    reject(closeError);
                    return;
                }
                if (wasConnected && typeof this.options.onClose === 'function') {
                    this.options.onClose({
                        intentional: this.intentionalClose,
                        code: event.code,
                        reason: event.reason || '',
                        exit: this.exitDetail,
                        error: this.lastError,
                    });
                }
            });
        }).catch((error) => {
            this._disposeRenderer();
            throw error;
        });
    }

    _disposeRenderer() {
        if (this.pingId) clearInterval(this.pingId);
        this.pingId = null;
        if (this.inputSubscription) this.inputSubscription.dispose();
        if (this.resizeSubscription) this.resizeSubscription.dispose();
        this.inputSubscription = null;
        this.resizeSubscription = null;
        if (this.renderer) this.renderer.dispose();
        this.renderer = null;
        this.receiveBuffer = new Uint8Array(0);
    }

    focus() {
        if (this.renderer) this.renderer.focus();
    }

    fit() {
        if (this.renderer) this.renderer.fit();
    }

    close() {
        if (this.disposed) return;
        this.disposed = true;
        this.intentionalClose = true;
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this._send('C');
        }
        if (this.socket && this.socket.readyState < WebSocket.CLOSING) {
            this.socket.close(1000, 'viewer closed');
        }
        this.connected = false;
        this._disposeRenderer();
    }
}

export async function openAxonosTerminal(options) {
    const client = new AxonosTerminalClient(options);
    await client.connect();
    return client;
}

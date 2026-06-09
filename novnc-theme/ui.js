/*
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/.
 *
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC Authors
 * Licensed under MPL 2.0 (see LICENSE.txt at noVNC repository).

 */

import * as Log from '../core/util/logging.js';
import _, { l10n } from './localization.js';
import * as Browser from '../core/util/browser.js';
import { setCapture, getPointerEvent } from '../core/util/events.js';
import KeyTable from "../core/input/keysym.js";
import keysyms from "../core/input/keysymdef.js";
import Keyboard from "../core/input/keyboard.js";
import RFB from "../core/rfb.js";
import * as WebUtil from "./webutil.js";

const isTouchDevice = (typeof Browser.isTouchDevice === 'function')
    ? Browser.isTouchDevice()
    : !!Browser.isTouchDevice;
const isSafari = (typeof Browser.isSafari === 'function')
    ? Browser.isSafari
    : () => !!Browser.isSafari;
const hasScrollbarGutter = (typeof Browser.hasScrollbarGutter === 'function')
    ? Browser.hasScrollbarGutter()
    : !!Browser.hasScrollbarGutter;
const dragThreshold = (Browser.dragThreshold !== undefined)
    ? Browser.dragThreshold
    : 10;
const setSetting = (name, value) => {
    if (typeof WebUtil.setSetting === 'function') {
        WebUtil.setSetting(name, value);
        return;
    }
    if (typeof WebUtil.writeSetting === 'function') {
        WebUtil.writeSetting(name, value);
    }
};

const PAGE_TITLE = "AxonOS Desktop";

const UI = {

    connected: false,
    desktopName: "",

    statusTimeout: null,
    hideKeyboardTimeout: null,
    idleControlbarTimeout: null,
    closeControlbarTimeout: null,

    controlbarGrabbed: false,
    controlbarDrag: false,
    controlbarMouseDownClientY: 0,
    controlbarMouseDownOffsetY: 0,

    lastKeyboardinput: null,
    defaultKeyboardinputLen: 100,

    inhibitReconnect: true,
    reconnectCallback: null,
    reconnectPassword: null,
    clipboardAutoSyncEnabled: false,
    clipboardAutoPollId: null,
    clipboardLastRemoteText: "",
    clipboardLastLocalText: "",
    clipboardApplyingRemoteText: false,
    /** Single in-flight `readText()` — overlapping reads hang after host paste. */
    clipboardReadInFlight: null,
    /** Host→remote clipboard only via the sidebar panel (avoids desktop input stalls). */
    clipboardPanelOnly: true,
    /** WebRTC: last time/text we pushed from host to remote (pull or Ctrl+V paste). */
    webrtcHostPushAt: 0,
    webrtcHostPushText: "",

    markHostClipboardSentToRemote(text) {
        if (typeof text !== 'string' || text.length === 0) {
            UI.webrtcHostPushAt = 0;
            UI.webrtcHostPushText = "";
            return;
        }
        UI.webrtcHostPushAt = Date.now();
        UI.webrtcHostPushText = text;
    },

    prime() {
        const initResult = (typeof WebUtil.initSettings === 'function')
            ? WebUtil.initSettings()
            : undefined;
        return Promise.resolve(initResult).then(() => {
            if (document.readyState === "interactive" || document.readyState === "complete") {
                return UI.start();
            }

            return new Promise((resolve, reject) => {
                document.addEventListener('DOMContentLoaded', () => UI.start().then(resolve).catch(reject));
            });
        });
    },

    // Render default UI and initialize settings menu
    start() {

        UI.initSettings();

        // Translate the DOM
        l10n.translateDOM();

        fetch('./package.json')
            .then((response) => {
                if (!response.ok) {
                    throw Error("" + response.status + " " + response.statusText);
                }
                return response.json();
            })
            .then((packageInfo) => {
                Array.from(document.getElementsByClassName('noVNC_version')).forEach(el => el.innerText = packageInfo.version);
            })
            .catch((err) => {
                Log.Error("Couldn't fetch package.json: " + err);
                Array.from(document.getElementsByClassName('noVNC_version_wrapper'))
                    .concat(Array.from(document.getElementsByClassName('noVNC_version_separator')))
                    .forEach(el => el.style.display = 'none');
            });

        // Adapt the interface for touch screen devices
        if (isTouchDevice) {
            document.documentElement.classList.add("noVNC_touch");
            // Remove the address bar
            setTimeout(() => window.scrollTo(0, 1), 100);
        }

        // Restore control bar position
        if (WebUtil.readSetting('controlbar_pos') === 'right') {
            UI.toggleControlbarSide();
        }

        UI.initFullscreen();

        // Setup event handlers
        UI.addControlbarHandlers();
        UI.addTouchSpecificHandlers();
        UI.addExtraKeysHandlers();
        UI.addMachineHandlers();
        UI.addAxonosSessionLifecycleHandlers();
        UI.addConnectionControlHandlers();
        UI.addClipboardHandlers();
        UI.addSettingsHandlers();
        document.getElementById("noVNC_status")
            .addEventListener('click', UI.hideStatus);

        // Bootstrap fallback input handler
        UI.keyboardinputReset();

        UI.openControlbar();

        UI.updateVisualState('init');

        document.documentElement.classList.remove("noVNC_loading");

        let autoconnect = WebUtil.getConfigVar('autoconnect', false);
        if (autoconnect === 'true' || autoconnect == '1') {
            autoconnect = true;
            UI.connect();
        } else {
            autoconnect = false;
            // Show the connect panel on first load unless autoconnecting
            UI.openConnectPanel();
        }

        UI.updateSessionControlButtons();

        return Promise.resolve(UI.rfb);
    },

    initFullscreen() {
        // Only show the button if fullscreen is properly supported
        // * Safari doesn't support alphanumerical input while in fullscreen
        if (!isSafari() &&
            (document.documentElement.requestFullscreen ||
             document.documentElement.mozRequestFullScreen ||
             document.documentElement.webkitRequestFullscreen ||
             document.body.msRequestFullscreen)) {
            document.getElementById('noVNC_fullscreen_button')
                .classList.remove("noVNC_hidden");
            UI.addFullscreenHandlers();
        }
    },

    initSettings() {
        // Logging selection dropdown
        const llevels = ['error', 'warn', 'info', 'debug'];
        for (let i = 0; i < llevels.length; i += 1) {
            UI.addOption(document.getElementById('noVNC_setting_logging'), llevels[i], llevels[i]);
        }

        // Settings with immediate effects
        UI.initSetting('logging', 'warn');
        UI.updateLogging();

        // if port == 80 (or 443) then it won't be present and should be
        // set manually
        let port = window.location.port;
        if (!port) {
            if (window.location.protocol.substring(0, 5) == 'https') {
                port = 443;
            } else if (window.location.protocol.substring(0, 4) == 'http') {
                port = 80;
            }
        }

        /* Populate the controls if defaults are provided in the URL */
        UI.initSetting('host', window.location.hostname);
        UI.initSetting('port', port);
        UI.initSetting('encrypt', (window.location.protocol === "https:"));
        UI.initSetting('view_clip', false);
        UI.initSetting('resize', 'scale');
        UI.initSetting('quality', 9);
        UI.initSetting('compression', 9);
        UI.initSetting('shared', true);
        UI.initSetting('view_only', false);
        UI.initSetting('show_dot', false);
        UI.initSetting('path', 'websockify');
        UI.initSetting('repeaterID', '');
        UI.initSetting('reconnect', false);
        UI.initSetting('reconnect_delay', 5000);

        UI.setupSettingLabels();
    },
    // Adds a link to the label elements on the corresponding input elements
    setupSettingLabels() {
        const labels = document.getElementsByTagName('LABEL');
        for (let i = 0; i < labels.length; i++) {
            const htmlFor = labels[i].htmlFor;
            if (htmlFor != '') {
                const elem = document.getElementById(htmlFor);
                if (elem) elem.label = labels[i];
            } else {
                // If 'for' isn't set, use the first input element child
                const children = labels[i].children;
                for (let j = 0; j < children.length; j++) {
                    if (children[j].form !== undefined) {
                        children[j].label = labels[i];
                        break;
                    }
                }
            }
        }
    },

/* ------^-------
*     /INIT
* ==============
* EVENT HANDLERS
* ------v------*/

    addControlbarHandlers() {
        document.getElementById("noVNC_control_bar")
            .addEventListener('mousemove', UI.activateControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('mouseup', UI.activateControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('mousedown', UI.activateControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('keydown', UI.activateControlbar);

        document.getElementById("noVNC_control_bar")
            .addEventListener('mousedown', UI.keepControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('keydown', UI.keepControlbar);

        document.getElementById("noVNC_view_drag_button")
            .addEventListener('click', UI.toggleViewDrag);

        document.getElementById("noVNC_control_bar_handle")
            .addEventListener('mousedown', UI.controlbarHandleMouseDown);
        document.getElementById("noVNC_control_bar_handle")
            .addEventListener('mouseup', UI.controlbarHandleMouseUp);
        document.getElementById("noVNC_control_bar_handle")
            .addEventListener('mousemove', UI.dragControlbarHandle);
        // resize events aren't available for elements
        window.addEventListener('resize', UI.updateControlbarHandle);

        const exps = document.getElementsByClassName("noVNC_expander");
        for (let i = 0;i < exps.length;i++) {
            exps[i].addEventListener('click', UI.toggleExpander);
        }
    },

    addTouchSpecificHandlers() {
        document.getElementById("noVNC_keyboard_button")
            .addEventListener('click', UI.toggleVirtualKeyboard);

        UI.touchKeyboard = new Keyboard(document.getElementById('noVNC_keyboardinput'));
        UI.touchKeyboard.onkeyevent = UI.keyEvent;
        UI.touchKeyboard.grab();
        document.getElementById("noVNC_keyboardinput")
            .addEventListener('input', UI.keyInput);
        document.getElementById("noVNC_keyboardinput")
            .addEventListener('focus', UI.onfocusVirtualKeyboard);
        document.getElementById("noVNC_keyboardinput")
            .addEventListener('blur', UI.onblurVirtualKeyboard);
        document.getElementById("noVNC_keyboardinput")
            .addEventListener('submit', () => false);

        document.documentElement
            .addEventListener('mousedown', UI.keepVirtualKeyboard, true);

        document.getElementById("noVNC_control_bar")
            .addEventListener('touchstart', UI.activateControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('touchmove', UI.activateControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('touchend', UI.activateControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('input', UI.activateControlbar);

        document.getElementById("noVNC_control_bar")
            .addEventListener('touchstart', UI.keepControlbar);
        document.getElementById("noVNC_control_bar")
            .addEventListener('input', UI.keepControlbar);

        document.getElementById("noVNC_control_bar_handle")
            .addEventListener('touchstart', UI.controlbarHandleMouseDown);
        document.getElementById("noVNC_control_bar_handle")
            .addEventListener('touchend', UI.controlbarHandleMouseUp);
        document.getElementById("noVNC_control_bar_handle")
            .addEventListener('touchmove', UI.dragControlbarHandle);
    },

    addExtraKeysHandlers() {
        document.getElementById("noVNC_toggle_extra_keys_button")
            .addEventListener('click', UI.toggleExtraKeys);
        document.getElementById("noVNC_toggle_ctrl_button")
            .addEventListener('click', UI.toggleCtrl);
        document.getElementById("noVNC_toggle_windows_button")
            .addEventListener('click', UI.toggleWindows);
        document.getElementById("noVNC_toggle_alt_button")
            .addEventListener('click', UI.toggleAlt);
        document.getElementById("noVNC_send_tab_button")
            .addEventListener('click', UI.sendTab);
        document.getElementById("noVNC_send_esc_button")
            .addEventListener('click', UI.sendEsc);
        document.getElementById("noVNC_send_ctrl_alt_del_button")
            .addEventListener('click', UI.sendCtrlAltDel);
    },

    addMachineHandlers() {
        const restartButton = document.getElementById("noVNC_restart_session_button");
        if (restartButton) {
            restartButton.addEventListener('click', UI.restartDesktopSession);
        }
        const endSessionButton = document.getElementById("noVNC_power_button");
        if (endSessionButton) {
            endSessionButton.addEventListener('click', UI.endSession);
        }
    },

    /** Tab close → release; F5/Ctrl+R → keep session (reload). */
    addAxonosSessionLifecycleHandlers() {
        if (window.axonosSessionLifecycleHandlersInstalled) {
            return;
        }
        window.axonosSessionLifecycleHandlersInstalled = true;

        window.addEventListener('keydown', (e) => {
            if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R'))) {
                try {
                    sessionStorage.setItem('axonos_nav', 'reload');
                } catch (err) { /* ignore */ }
            }
        }, true);

        window.addEventListener('beforeunload', () => {
            try {
                if (!sessionStorage.getItem('axonos_nav')) {
                    sessionStorage.setItem('axonos_nav', 'close');
                }
            } catch (err) { /* ignore */ }
        });

        window.addEventListener('pagehide', (e) => {
            if (e.persisted) {
                return;
            }
            let nav = 'close';
            try {
                nav = sessionStorage.getItem('axonos_nav') || 'close';
                sessionStorage.removeItem('axonos_nav');
            } catch (err) { /* ignore */ }
            if (nav === 'reload') {
                return;
            }
            if (!UI._axonosSessionOwnsServerSlot()) {
                return;
            }
            UI._axonosReleaseSessionBeacon();
        });
    },

    addConnectionControlHandlers() {
        document.getElementById("noVNC_disconnect_button")
            .addEventListener('click', UI.detach);
        document.getElementById("noVNC_connect_button")
            .addEventListener('click', UI.connect);
        document.getElementById("noVNC_cancel_reconnect_button")
            .addEventListener('click', UI.cancelReconnect);

        document.getElementById("noVNC_credentials_button")
            .addEventListener('click', UI.setCredentials);
    },

    addClipboardHandlers() {
        const clipboardButton = document.getElementById("noVNC_clipboard_button");
        if (clipboardButton) {
            clipboardButton.addEventListener('click', UI.toggleClipboardPanel);
        }
        const clipboardText = document.getElementById("noVNC_clipboard_text");
        if (clipboardText) {
            clipboardText.addEventListener('change', UI.clipboardSend);
            clipboardText.addEventListener('input', UI.clipboardSend);
        }
        const clipboardClearButton = document.getElementById("noVNC_clipboard_clear_button");
        if (clipboardClearButton) {
            clipboardClearButton.addEventListener('click', UI.clipboardClear);
        }
        if (!UI.clipboardPanelOnly) {
            document.addEventListener('paste', UI.handleLocalClipboardPaste, true);
        }
    },

    clipboardHasBrowserPermission() {
        return !!(navigator && navigator.clipboard && typeof navigator.clipboard.readText === 'function');
    },

    clipboardLooksSelectableTarget(target) {
        if (!target) return false;
        const tag = target.tagName ? target.tagName.toLowerCase() : '';
        if (tag === 'textarea') return true;
        if (tag === 'input') {
            const type = (target.type || '').toLowerCase();
            return type === '' || type === 'text' || type === 'search' || type === 'url' ||
                type === 'tel' || type === 'email' || type === 'password';
        }
        return target.isContentEditable === true;
    },

    setClipboardTextarea(text) {
        const clipboardInput = document.getElementById('noVNC_clipboard_text');
        if (clipboardInput.value === text) return;
        UI.clipboardApplyingRemoteText = true;
        clipboardInput.value = text;
        UI.clipboardApplyingRemoteText = false;
    },

    syncClipboardPanelValueFromLocal() {
        if (!UI.clipboardHasBrowserPermission()) return Promise.resolve(false);
        return UI.pullLocalClipboardToRemote({ timeoutMs: 800, panelOnly: true });
    },

    pushRemoteClipboardToLocal(text) {
        if (!UI.clipboardHasBrowserPermission()) return Promise.resolve(false);
        return navigator.clipboard.writeText(text)
            .then(() => {
                UI.clipboardLastLocalText = text;
                return true;
            })
            .catch(() => false);
    },

    pasteClipboardToRemote(text, pasteNow) {
        if (!UI.rfb || typeof UI.rfb.clipboardPasteFrom !== 'function') {
            if (typeof window.axonosWebRtcPasteClipboard === 'function') {
                const ok = window.axonosWebRtcPasteClipboard(text, pasteNow === true);
                if (ok && text && typeof UI.markHostClipboardSentToRemote === 'function') {
                    UI.markHostClipboardSentToRemote(text);
                }
                return ok;
            }
            return false;
        }
        UI.rfb.clipboardPasteFrom(text);
        return true;
    },

    _pushLocalClipboardText(text) {
        if (typeof text !== 'string') return false;
        const maxChars = 512 * 1024;
        if (text.length > maxChars) {
            text = text.slice(0, maxChars);
        }
        // Dedupe only on text we have already pushed to the remote.
        if (text === UI.clipboardLastLocalText) {
            return false;
        }
        UI.setClipboardTextarea(text);
        const pushed = UI.pasteClipboardToRemote(text);
        if (pushed) {
            UI.clipboardLastLocalText = text;
            UI.clipboardLastRemoteText = text;
            UI.markHostClipboardSentToRemote(text);
        }
        return pushed;
    },

    pullLocalClipboardToRemote(opts) {
        if (!UI.connected || !UI.clipboardHasBrowserPermission()) {
            return Promise.resolve(false);
        }
        const knownText = opts && typeof opts.knownText === 'string' ? opts.knownText : null;
        if (knownText !== null) {
            return Promise.resolve(UI._pushLocalClipboardText(knownText));
        }
        if (UI.clipboardReadInFlight) {
            return UI.clipboardReadInFlight;
        }
        const timeoutMs = (opts && typeof opts.timeoutMs === 'number') ? opts.timeoutMs : 0;
        const abort = typeof AbortController !== 'undefined' ? new AbortController() : null;
        let readP;
        try {
            readP = abort
                ? navigator.clipboard.readText({ signal: abort.signal })
                : navigator.clipboard.readText();
        } catch {
            return Promise.resolve(false);
        }
        let timeoutId = null;
        const raced = timeoutMs > 0
            ? Promise.race([
                readP,
                new Promise((resolve) => {
                    timeoutId = window.setTimeout(() => {
                        if (abort) {
                            try { abort.abort(); } catch { /* ignore */ }
                        }
                        resolve('__clipboard_timeout__');
                    }, timeoutMs);
                }),
            ])
            : readP;
        const p = raced
            .then((text) => {
                if (text === '__clipboard_timeout__') return false;
                if (typeof text !== 'string') return false;
                if (opts && opts.panelOnly === true) {
                    UI.setClipboardTextarea(text);
                    return true;
                }
                return UI._pushLocalClipboardText(text);
            })
            .catch(() => false)
            .finally(() => {
                if (timeoutId !== null) {
                    clearTimeout(timeoutId);
                }
                if (UI.clipboardReadInFlight === p) {
                    UI.clipboardReadInFlight = null;
                }
            });
        UI.clipboardReadInFlight = p;
        return p;
    },

    startClipboardAutoSync() {
        UI.stopClipboardAutoSync();
        if (UI.clipboardPanelOnly) return;
        UI.clipboardAutoSyncEnabled = UI.clipboardHasBrowserPermission();
        if (!UI.clipboardAutoSyncEnabled) return;
        UI.pullLocalClipboardToRemote({ timeoutMs: 800 });
        UI.clipboardAutoPollId = window.setInterval(
            () => UI.pullLocalClipboardToRemote({ timeoutMs: 800 }),
            1500,
        );
    },

    stopClipboardAutoSync() {
        UI.clipboardAutoSyncEnabled = false;
        if (UI.clipboardAutoPollId) {
            clearInterval(UI.clipboardAutoPollId);
            UI.clipboardAutoPollId = null;
        }
    },

    handleLocalClipboardPaste(e) {
        if (!UI.connected || UI.clipboardPanelOnly) return;
        // WebRTC path registers its own capture-phase paste handler.
        if (typeof window.axonosWebRtcPasteClipboard === 'function') {
            return;
        }
        const active = document.activeElement;
        if (UI.clipboardLooksSelectableTarget(active)) return;
        const clipData = e.clipboardData || window.clipboardData;
        if (!clipData) return;
        const text = clipData.getData('text/plain');
        if (!text || text === UI.clipboardLastRemoteText) return;
        UI.clipboardLastLocalText = text;
        UI.clipboardLastRemoteText = text;
        UI.setClipboardTextarea(text);
        UI.pasteClipboardToRemote(text, true);
    },

    // Add a call to save settings when the element changes,
    // unless the optional parameter changeFunc is used instead.
    addSettingChangeHandler(name, changeFunc) {
        const settingElem = document.getElementById("noVNC_setting_" + name);
        if (changeFunc === undefined) {
            changeFunc = () => UI.saveSetting(name);
        }
        settingElem.addEventListener('change', changeFunc);
    },

    addSettingsHandlers() {
        document.getElementById("noVNC_settings_button")
            .addEventListener('click', UI.toggleSettingsPanel);

        UI.addSettingChangeHandler('encrypt');
        UI.addSettingChangeHandler('resize');
        UI.addSettingChangeHandler('resize', UI.applyResizeMode);
        UI.addSettingChangeHandler('resize', UI.updateViewClip);
        UI.addSettingChangeHandler('quality');
        UI.addSettingChangeHandler('quality', UI.updateQuality);
        UI.addSettingChangeHandler('compression');
        UI.addSettingChangeHandler('compression', UI.updateCompression);
        UI.addSettingChangeHandler('view_clip');
        UI.addSettingChangeHandler('view_clip', UI.updateViewClip);
        UI.addSettingChangeHandler('shared');
        UI.addSettingChangeHandler('view_only');
        UI.addSettingChangeHandler('view_only', UI.updateViewOnly);
        UI.addSettingChangeHandler('show_dot');
        UI.addSettingChangeHandler('show_dot', UI.updateShowDotCursor);
        UI.addSettingChangeHandler('host');
        UI.addSettingChangeHandler('port');
        UI.addSettingChangeHandler('path');
        UI.addSettingChangeHandler('repeaterID');
        UI.addSettingChangeHandler('logging');
        UI.addSettingChangeHandler('logging', UI.updateLogging);
        UI.addSettingChangeHandler('reconnect');
        UI.addSettingChangeHandler('reconnect_delay');
    },

    addFullscreenHandlers() {
        document.getElementById("noVNC_fullscreen_button")
            .addEventListener('click', UI.toggleFullscreen);

        window.addEventListener('fullscreenchange', UI.updateFullscreenButton);
        window.addEventListener('mozfullscreenchange', UI.updateFullscreenButton);
        window.addEventListener('webkitfullscreenchange', UI.updateFullscreenButton);
        window.addEventListener('msfullscreenchange', UI.updateFullscreenButton);
    },

/* ------^-------
 * /EVENT HANDLERS
 * ==============
 *     VISUAL
 * ------v------*/

    // Disable/enable controls depending on connection state
    updateVisualState(state) {

        document.documentElement.classList.remove("noVNC_connecting");
        document.documentElement.classList.remove("noVNC_connected");
        document.documentElement.classList.remove("noVNC_disconnecting");
        document.documentElement.classList.remove("noVNC_reconnecting");

        const transitionElem = document.getElementById("noVNC_transition_text");
        switch (state) {
            case 'init':
                break;
            case 'connecting':
                transitionElem.textContent = _("Connecting...");
                document.documentElement.classList.add("noVNC_connecting");
                break;
            case 'connected':
                document.documentElement.classList.add("noVNC_connected");
                break;
            case 'disconnecting':
                transitionElem.textContent = _("Disconnecting...");
                document.documentElement.classList.add("noVNC_disconnecting");
                break;
            case 'disconnected':
                break;
            case 'reconnecting':
                transitionElem.textContent = _("Reconnecting...");
                document.documentElement.classList.add("noVNC_reconnecting");
                break;
            default:
                Log.Error("Invalid visual state: " + state);
                UI.showStatus(_("Internal error"), 'error');
                return;
        }

        if (UI.connected) {
            UI.updateViewClip();

            UI.disableSetting('encrypt');
            UI.disableSetting('shared');
            UI.disableSetting('host');
            UI.disableSetting('port');
            UI.disableSetting('path');
            UI.disableSetting('repeaterID');

            // Hide the controlbar after 2 seconds
            UI.closeControlbarTimeout = setTimeout(UI.closeControlbar, 2000);
        } else {
            UI.enableSetting('encrypt');
            UI.enableSetting('shared');
            UI.enableSetting('host');
            UI.enableSetting('port');
            UI.enableSetting('path');
            UI.enableSetting('repeaterID');
            UI.updateSessionControlButtons();
            UI.keepControlbar();
        }

        // State change closes dialogs as they may not be relevant
        // anymore
        UI.closeAllPanels();
        document.getElementById('noVNC_credentials_dlg')
            .classList.remove('noVNC_open');
    },

    showStatus(text, statusType, time) {
        const statusElem = document.getElementById('noVNC_status');

        if (typeof statusType === 'undefined') {
            statusType = 'normal';
        }

        // Don't overwrite more severe visible statuses and never
        // errors. Only shows the first error.
        if (statusElem.classList.contains("noVNC_open")) {
            if (statusElem.classList.contains("noVNC_status_error")) {
                return;
            }
            if (statusElem.classList.contains("noVNC_status_warn") &&
                statusType === 'normal') {
                return;
            }
        }

        clearTimeout(UI.statusTimeout);

        switch (statusType) {
            case 'error':
                statusElem.classList.remove("noVNC_status_warn");
                statusElem.classList.remove("noVNC_status_normal");
                statusElem.classList.add("noVNC_status_error");
                break;
            case 'warning':
            case 'warn':
                statusElem.classList.remove("noVNC_status_error");
                statusElem.classList.remove("noVNC_status_normal");
                statusElem.classList.add("noVNC_status_warn");
                break;
            case 'normal':
            case 'info':
            default:
                statusElem.classList.remove("noVNC_status_error");
                statusElem.classList.remove("noVNC_status_warn");
                statusElem.classList.add("noVNC_status_normal");
                break;
        }

        statusElem.textContent = text;
        statusElem.classList.add("noVNC_open");

        // If no time was specified, show the status for 1.5 seconds
        if (typeof time === 'undefined') {
            time = 1500;
        }

        // Error messages do not timeout
        if (statusType !== 'error') {
            UI.statusTimeout = window.setTimeout(UI.hideStatus, time);
        }
    },

    hideStatus() {
        clearTimeout(UI.statusTimeout);
        document.getElementById('noVNC_status').classList.remove("noVNC_open");
    },

    focusRemoteDesktop() {
        if (UI.rfb && typeof UI.rfb.focus === 'function') {
            UI.rfb.focus();
            return;
        }
        const webrtcVideo = document.getElementById('axonos_webrtc_video');
        if (webrtcVideo && typeof webrtcVideo.focus === 'function') {
            webrtcVideo.focus();
        }
    },

    /** Release stuck WebRTC mouse state when local UI (clipboard panel, etc.) takes focus. */
    releaseWebRtcPointerState() {
        if (typeof window.axonosWebRtcReleasePointerState === 'function') {
            window.axonosWebRtcReleasePointerState();
        }
    },

    activateControlbar(event) {
        clearTimeout(UI.idleControlbarTimeout);
        // We manipulate the anchor instead of the actual control
        // bar in order to avoid creating new a stacking group
        document.getElementById('noVNC_control_bar_anchor')
            .classList.remove("noVNC_idle");
        UI.idleControlbarTimeout = window.setTimeout(UI.idleControlbar, 2000);
    },

    idleControlbar() {
        // Don't fade if a child of the control bar has focus
        if (document.getElementById('noVNC_control_bar')
            .contains(document.activeElement) && document.hasFocus()) {
            UI.activateControlbar();
            return;
        }

        document.getElementById('noVNC_control_bar_anchor')
            .classList.add("noVNC_idle");
    },

    keepControlbar() {
        clearTimeout(UI.closeControlbarTimeout);
    },

    openControlbar() {
        document.getElementById('noVNC_control_bar')
            .classList.add("noVNC_open");
    },

    closeControlbar() {
        UI.closeAllPanels();
        document.getElementById('noVNC_control_bar')
            .classList.remove("noVNC_open");
        UI.focusRemoteDesktop();
    },

    toggleControlbar() {
        if (document.getElementById('noVNC_control_bar')
            .classList.contains("noVNC_open")) {
            UI.closeControlbar();
        } else {
            UI.openControlbar();
        }
    },

    toggleControlbarSide() {
        // Temporarily disable animation, if bar is displayed, to avoid weird
        // movement. The transitionend-event will not fire when display=none.
        const bar = document.getElementById('noVNC_control_bar');
        const barDisplayStyle = window.getComputedStyle(bar).display;
        if (barDisplayStyle !== 'none') {
            bar.style.transitionDuration = '0s';
            bar.addEventListener('transitionend', () => bar.style.transitionDuration = '');
        }

        const anchor = document.getElementById('noVNC_control_bar_anchor');
        if (anchor.classList.contains("noVNC_right")) {
            WebUtil.writeSetting('controlbar_pos', 'left');
            anchor.classList.remove("noVNC_right");
        } else {
            WebUtil.writeSetting('controlbar_pos', 'right');
            anchor.classList.add("noVNC_right");
        }

        // Consider this a movement of the handle
        UI.controlbarDrag = true;
    },

    showControlbarHint(show) {
        const hint = document.getElementById('noVNC_control_bar_hint');
        if (show) {
            hint.classList.add("noVNC_active");
        } else {
            hint.classList.remove("noVNC_active");
        }
    },

    dragControlbarHandle(e) {
        if (!UI.controlbarGrabbed) return;

        const ptr = getPointerEvent(e);

        const anchor = document.getElementById('noVNC_control_bar_anchor');
        if (ptr.clientX < (window.innerWidth * 0.1)) {
            if (anchor.classList.contains("noVNC_right")) {
                UI.toggleControlbarSide();
            }
        } else if (ptr.clientX > (window.innerWidth * 0.9)) {
            if (!anchor.classList.contains("noVNC_right")) {
                UI.toggleControlbarSide();
            }
        }

        if (!UI.controlbarDrag) {
            const dragDistance = Math.abs(ptr.clientY - UI.controlbarMouseDownClientY);

            if (dragDistance < dragThreshold) return;

            UI.controlbarDrag = true;
        }

        const eventY = ptr.clientY - UI.controlbarMouseDownOffsetY;

        UI.moveControlbarHandle(eventY);

        e.preventDefault();
        e.stopPropagation();
        UI.keepControlbar();
        UI.activateControlbar();
    },

    // Move the handle but don't allow any position outside the bounds
    moveControlbarHandle(viewportRelativeY) {
        const handle = document.getElementById("noVNC_control_bar_handle");
        const handleHeight = handle.getBoundingClientRect().height;
        const controlbarBounds = document.getElementById("noVNC_control_bar")
            .getBoundingClientRect();
        const margin = 10;

        // These heights need to be non-zero for the below logic to work
        if (handleHeight === 0 || controlbarBounds.height === 0) {
            return;
        }

        let newY = viewportRelativeY;

        // Check if the coordinates are outside the control bar
        if (newY < controlbarBounds.top + margin) {
            // Force coordinates to be below the top of the control bar
            newY = controlbarBounds.top + margin;

        } else if (newY > controlbarBounds.top +
                   controlbarBounds.height - handleHeight - margin) {
            // Force coordinates to be above the bottom of the control bar
            newY = controlbarBounds.top +
                controlbarBounds.height - handleHeight - margin;
        }

        // Corner case: control bar too small for stable position
        if (controlbarBounds.height < (handleHeight + margin * 2)) {
            newY = controlbarBounds.top +
                (controlbarBounds.height - handleHeight) / 2;
        }

        // The transform needs coordinates that are relative to the parent
        const parentRelativeY = newY - controlbarBounds.top;
        handle.style.transform = "translateY(" + parentRelativeY + "px)";
    },

    updateControlbarHandle() {
        // Since the control bar is fixed on the viewport and not the page,
        // the move function expects coordinates relative the the viewport.
        const handle = document.getElementById("noVNC_control_bar_handle");
        const handleBounds = handle.getBoundingClientRect();
        UI.moveControlbarHandle(handleBounds.top);
    },

    controlbarHandleMouseUp(e) {
        if ((e.type == "mouseup") && (e.button != 0)) return;

        // mouseup and mousedown on the same place toggles the controlbar
        if (UI.controlbarGrabbed && !UI.controlbarDrag) {
            UI.toggleControlbar();
            e.preventDefault();
            e.stopPropagation();
            UI.keepControlbar();
            UI.activateControlbar();
        }
        UI.controlbarGrabbed = false;
        UI.showControlbarHint(false);
    },

    controlbarHandleMouseDown(e) {
        if ((e.type == "mousedown") && (e.button != 0)) return;

        const ptr = getPointerEvent(e);

        const handle = document.getElementById("noVNC_control_bar_handle");
        const bounds = handle.getBoundingClientRect();

        // Touch events have implicit capture
        if (e.type === "mousedown") {
            setCapture(handle);
        }

        UI.controlbarGrabbed = true;
        UI.controlbarDrag = false;

        UI.showControlbarHint(true);

        UI.controlbarMouseDownClientY = ptr.clientY;
        UI.controlbarMouseDownOffsetY = ptr.clientY - bounds.top;
        e.preventDefault();
        e.stopPropagation();
        UI.keepControlbar();
        UI.activateControlbar();
    },

    toggleExpander(e) {
        if (this.classList.contains("noVNC_open")) {
            this.classList.remove("noVNC_open");
        } else {
            this.classList.add("noVNC_open");
        }
    },

/* ------^-------
 *    /VISUAL
 * ==============
 *    SETTINGS
 * ------v------*/

    // Initial page load read/initialization of settings
    initSetting(name, defVal) {
        // Check Query string followed by cookie
        let val = WebUtil.getConfigVar(name);
        if (val === null) {
            val = WebUtil.readSetting(name, defVal);
        }
        setSetting(name, val);
        UI.updateSetting(name);
        return val;
    },

    // Set the new value, update and disable form control setting
    forceSetting(name, val) {
        setSetting(name, val);
        UI.updateSetting(name);
        UI.disableSetting(name);
    },

    // Update cookie and form control setting. If value is not set, then
    // updates from control to current cookie setting.
    updateSetting(name) {

        // Update the settings control
        let value = UI.getSetting(name);

        const ctrl = document.getElementById('noVNC_setting_' + name);
        if (ctrl.type === 'checkbox') {
            ctrl.checked = value;

        } else if (typeof ctrl.options !== 'undefined') {
            for (let i = 0; i < ctrl.options.length; i += 1) {
                if (ctrl.options[i].value === value) {
                    ctrl.selectedIndex = i;
                    break;
                }
            }
        } else {
            ctrl.value = value;
        }
    },

    // Save control setting to cookie
    saveSetting(name) {
        const ctrl = document.getElementById('noVNC_setting_' + name);
        let val;
        if (ctrl.type === 'checkbox') {
            val = ctrl.checked;
        } else if (typeof ctrl.options !== 'undefined') {
            val = ctrl.options[ctrl.selectedIndex].value;
        } else {
            val = ctrl.value;
        }
        WebUtil.writeSetting(name, val);
        //Log.Debug("Setting saved '" + name + "=" + val + "'");
        return val;
    },

    // Read form control compatible setting from cookie
    getSetting(name) {
        const ctrl = document.getElementById('noVNC_setting_' + name);
        let val = WebUtil.readSetting(name);
        if (typeof val !== 'undefined' && val !== null && ctrl.type === 'checkbox') {
            if (val.toString().toLowerCase() in {'0': 1, 'no': 1, 'false': 1}) {
                val = false;
            } else {
                val = true;
            }
        }
        return val;
    },

    // These helpers compensate for the lack of parent-selectors and
    // previous-sibling-selectors in CSS which are needed when we want to
    // disable the labels that belong to disabled input elements.
    disableSetting(name) {
        const ctrl = document.getElementById('noVNC_setting_' + name);
        ctrl.disabled = true;
        ctrl.label.classList.add('noVNC_disabled');
    },

    enableSetting(name) {
        const ctrl = document.getElementById('noVNC_setting_' + name);
        ctrl.disabled = false;
        ctrl.label.classList.remove('noVNC_disabled');
    },

/* ------^-------
 *   /SETTINGS
 * ==============
 *    PANELS
 * ------v------*/

    closeAllPanels() {
        UI.closeSettingsPanel();
        UI.closeClipboardPanel();
        UI.closeExtraKeys();
    },

/* ------^-------
 *   /PANELS
 * ==============
 * SETTINGS (panel)
 * ------v------*/

    openSettingsPanel() {
        UI.closeAllPanels();
        UI.releaseWebRtcPointerState();
        UI.openControlbar();

        // Refresh UI elements from saved cookies
        UI.updateSetting('encrypt');
        UI.updateSetting('view_clip');
        UI.updateSetting('resize');
        UI.updateSetting('quality');
        UI.updateSetting('compression');
        UI.updateSetting('shared');
        UI.updateSetting('view_only');
        UI.updateSetting('path');
        UI.updateSetting('repeaterID');
        UI.updateSetting('logging');
        UI.updateSetting('reconnect');
        UI.updateSetting('reconnect_delay');

        document.getElementById('noVNC_settings')
            .classList.add("noVNC_open");
        document.getElementById('noVNC_settings_button')
            .classList.add("noVNC_selected");
    },

    closeSettingsPanel() {
        document.getElementById('noVNC_settings')
            .classList.remove("noVNC_open");
        document.getElementById('noVNC_settings_button')
            .classList.remove("noVNC_selected");
    },

    toggleSettingsPanel() {
        if (document.getElementById('noVNC_settings')
            .classList.contains("noVNC_open")) {
            UI.closeSettingsPanel();
        } else {
            UI.openSettingsPanel();
        }
    },

/* ------^-------
 *   /SETTINGS
 * ==============
 *  SESSION CONTROLS
 * ------v------*/

    endSession() {
        if (!UI.connected && !window.axonosSessionDetached) {
            return;
        }
        const storageEnabled = window.axonosConfig && window.axonosConfig.persistent_storage_enabled;
        const storageCost = window.axonosConfig && window.axonosConfig.persistent_storage_gb_hour_cost_minutes != null
            ? window.axonosConfig.persistent_storage_gb_hour_cost_minutes
            : 0.05;
        const limitAbs = window.axonosConfig && window.axonosConfig.persistent_storage_min_balance_limit_minutes != null
            ? Math.abs(window.axonosConfig.persistent_storage_min_balance_limit_minutes)
            : 1440.0;
        const limitHours = limitAbs / 60;
        const limitStr = (limitAbs % 60 === 0)
            ? limitHours + " hour" + (limitHours !== 1 ? "s" : "")
            : limitAbs + " minute" + (limitAbs !== 1 ? "s" : "");
        const msg = storageEnabled
            ? _("End session now?\n\nThis stops billing for compute, ends your session, and tears down the desktop container. Your files in the home folder are safely saved (offline storage is charged at " + storageCost + " minutes per GB/hour, accruing as debt when your balance is empty). To avoid volume deletion, clear your debt before it exceeds " + limitStr + ".")
            : _("End session now?\n\nThis stops billing, ends your session, and removes your remote desktop. Unsaved work may be lost.");
        const confirmed = window.confirm(msg);
        if (!confirmed) {
            return;
        }
        window.axonosSessionDetached = false;
        UI.disconnect();
    },

    detach() {
        if (!UI.connected) {
            return;
        }
        const confirmed = window.confirm(
            _("Detach from the remote view?\n\nYou return to the home screen. Your desktop keeps running and prepaid minutes keep counting while this tab stays open.\n\nUse End session or close this tab when you are fully done.")
        );
        if (!confirmed) {
            return;
        }
        UI.disconnect({ skipRelease: true, detach: true });
    },

    restartDesktopSession() {
        if (!UI.connected && !window.axonosSessionDetached) return;
        const wallet = window.verifiedWalletAddress;
        if (!wallet) {
            UI.showStatus(_("Wallet verification required"), 'error');
            return;
        }

        const confirmed = window.confirm(
            _("Restart desktop session now? Open apps in the remote desktop may close.")
        );
        if (!confirmed) return;

        UI.showStatus(_("Restarting desktop session..."), 'normal', 2500);

        const url = new URL('/api/session/restart', window.location.origin).toString();
        const headers = {
            'Content-Type': 'application/json',
            'X-Wallet-Address': wallet,
        };
        if (window.verifiedWalletAuthToken) {
            headers['X-AXGT-Auth-Token'] = window.verifiedWalletAuthToken;
        }

        fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers,
            body: JSON.stringify({ wallet_address: wallet }),
        }).then((response) => {
            const ct = (response.headers.get('content-type') || '');
            if (!ct.includes('application/json')) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return {};
            }
            return response.json();
        }).then((data) => {
            if (!data || data.restarted !== true) {
                const reason = data && data.reason ? String(data.reason) : _('Restart was not accepted');
                throw new Error(reason);
            }
            UI.closeSettingsPanel();
            UI.showStatus(_("Desktop session restart requested"), 'normal');
            UI.focusRemoteDesktop();
        }).catch((err) => {
            Log.Error("Desktop restart request failed: " + err);
            UI.showStatus(_("Could not restart desktop session"), 'error');
        });
    },

    _axonosViewerViewOnly() {
        return !!(UI.rfb && UI.rfb.viewOnly);
    },

    updateSessionControlButtons() {
        const endBtn = document.getElementById('noVNC_power_button');
        const detachBtn = document.getElementById('noVNC_disconnect_button');
        if (!endBtn || !detachBtn) {
            return;
        }
        const viewOnly = UI._axonosViewerViewOnly();
        const showEnd = (UI.connected || window.axonosSessionDetached) && !viewOnly;
        const showDetach = UI.connected && !window.axonosSessionDetached && !viewOnly;
        endBtn.classList.toggle('noVNC_hidden', !showEnd);
        detachBtn.classList.toggle('noVNC_hidden', !showDetach);
    },

    /** @deprecated alias */
    updatePowerButton() {
        UI.updateSessionControlButtons();
    },

/* ------^-------
 *    /SESSION CONTROLS
 * ==============
 *   CLIPBOARD
 * ------v------*/

    openClipboardPanel() {
        UI.closeAllPanels();
        UI.releaseWebRtcPointerState();
        UI.openControlbar();

        document.getElementById('noVNC_clipboard')
            .classList.add("noVNC_open");
        document.getElementById('noVNC_clipboard_button')
            .classList.add("noVNC_selected");

        if (UI.clipboardPanelOnly) {
            const sync = UI.syncClipboardPanelValueFromLocal();
            if (sync && typeof sync.then === 'function') {
                sync.then((ok) => {
                    if (!ok) return;
                    const text = document.getElementById('noVNC_clipboard_text').value;
                    if (text && text !== UI.clipboardLastRemoteText) {
                        UI.clipboardSend();
                    }
                });
            }
        }
    },

    closeClipboardPanel() {
        document.getElementById('noVNC_clipboard')
            .classList.remove("noVNC_open");
        document.getElementById('noVNC_clipboard_button')
            .classList.remove("noVNC_selected");
    },

    toggleClipboardPanel() {
        if (document.getElementById('noVNC_clipboard')
            .classList.contains("noVNC_open")) {
            UI.closeClipboardPanel();
        } else {
            UI.openClipboardPanel();
        }
    },

    clipboardReceive(e) {
        const text = (e && e.detail && typeof e.detail.text === 'string') ? e.detail.text : "";
        Log.Debug(">> UI.clipboardReceive: " + text.substr(0, 40) + "...");
        UI.clipboardLastRemoteText = text;
        UI.setClipboardTextarea(text);
        UI.pushRemoteClipboardToLocal(text);
        Log.Debug("<< UI.clipboardReceive");
    },

    clipboardClear() {
        UI.clipboardLastRemoteText = "";
        UI.clipboardLastLocalText = "";
        UI.markHostClipboardSentToRemote("");
        UI.setClipboardTextarea("");
        UI.pushRemoteClipboardToLocal("");
        UI.pasteClipboardToRemote("");
    },

    clipboardSend() {
        const text = document.getElementById('noVNC_clipboard_text').value;
        if (UI.clipboardApplyingRemoteText) return;
        Log.Debug(">> UI.clipboardSend: " + text.substr(0, 40) + "...");
        UI.clipboardLastRemoteText = text;
        UI.clipboardLastLocalText = text;
        UI.pushRemoteClipboardToLocal(text);
        UI.pasteClipboardToRemote(text);
        Log.Debug("<< UI.clipboardSend");
    },

/* ------^-------
 *  /CLIPBOARD
 * ==============
 *  CONNECTION
 * ------v------*/

    openConnectPanel() {
        document.getElementById('noVNC_connect_dlg')
            .classList.add("noVNC_open");
    },

    closeConnectPanel() {
        document.getElementById('noVNC_connect_dlg')
            .classList.remove("noVNC_open");
    },

    /**
     * Clear error banner, pending reconnect timer, and reconnect inhibition so the next
     * "Launch GPU-Native Desktop" can proceed. Mirrors the intent of the credit-exhaustion
     * path (reset gate) but without clearing wallet auth — use after leaving the queue or
     * when recovering from a failed WS (e.g. 1006) before retrying.
     */
    axonosResetDesktopGateForRetry() {
        UI._axonosCancelWebRtcClient();
        UI.hideStatus();
        if (UI.reconnectCallback !== null) {
            clearTimeout(UI.reconnectCallback);
            UI.reconnectCallback = null;
        }
        UI.inhibitReconnect = false;
        const keepBilling = typeof UI._axgtSessionBillingActive === 'function'
            && UI._axgtSessionBillingActive();
        if (typeof UI.rfb !== 'undefined' && UI.rfb) {
            try {
                UI.rfb.disconnect();
            } catch (e) { /* ignore */ }
            UI.rfb = undefined;
            UI.connected = false;
        }
        if (!keepBilling && UI._axgtStatusPollId) {
            clearInterval(UI._axgtStatusPollId);
            UI._axgtStatusPollId = null;
        }
        const overlay = document.getElementById('axonos_usage_overlay');
        if (!overlay || !overlay.classList.contains('axonos-usage-overlay--locked')) {
            UI._axgtUpdateUsageOverlay('hidden');
        }
        if (keepBilling) {
            UI._axgtStartSessionBillingPoll();
            UI.updateSessionControlButtons();
            return;
        }
        UI.updateVisualState('disconnected');
        UI.updateSessionControlButtons();
    },

    /** POST /api/session/claim — required by AxonOS gate before WebSocket upgrade. */
    _axonosFetchSessionClaim() {
        const wallet = window.verifiedWalletAddress;
        if (!wallet) {
            return Promise.resolve({ granted: false, reason: 'No wallet' });
        }
        const payload = { wallet_address: wallet };
        if (!window.axonosPausedResume && !window.axonosDetachedSession) {
            payload.requested_profile = (typeof window.axonosGetRequestedProfile === 'function')
                ? window.axonosGetRequestedProfile()
                : 'small';
        }
        const url = new URL('/api/session/claim', window.location.origin).toString();
        const headers = {
            'Content-Type': 'application/json',
            'X-Wallet-Address': wallet,
        };
        if (window.verifiedWalletAuthToken) {
            headers['X-AXGT-Auth-Token'] = window.verifiedWalletAuthToken;
        }
        return fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers,
            body: JSON.stringify(payload),
        }).then((r) => {
            const ct = (r.headers.get('content-type') || '');
            if (!ct.includes('application/json')) {
                if (!r.ok) {
                    throw new Error('HTTP ' + r.status);
                }
                return {};
            }
            return r.json();
        });
    },

    _axonosReleaseSessionHeaders() {
        const wallet = window.verifiedWalletAddress;
        if (!wallet) {
            return null;
        }
        const headers = {
            'Content-Type': 'application/json',
            'X-Wallet-Address': wallet,
        };
        if (window.verifiedWalletAuthToken) {
            headers['X-AXGT-Auth-Token'] = window.verifiedWalletAuthToken;
        }
        return headers;
    },

    _axonosSessionOwnsServerSlot() {
        return !!(window.axonosSessionDetached ||
            UI.connected ||
            UI._axgtStatusPollId);
    },

    /** Fire-and-forget release for tab close (pagehide). */
    _axonosReleaseSessionBeacon() {
        const wallet = window.verifiedWalletAddress;
        const headers = UI._axonosReleaseSessionHeaders();
        if (!wallet || !headers) {
            return;
        }
        const url = new URL('/api/session/release', window.location.origin).toString();
        const body = JSON.stringify({ wallet_address: wallet });
        fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers,
            body,
            keepalive: true,
        }).catch(() => {
            try {
                if (typeof navigator.sendBeacon === 'function') {
                    navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
                }
            } catch (err) { /* ignore */ }
        });
    },

    /** POST /api/session/release — best effort on user-triggered disconnect. */
    _axonosReleaseSessionBestEffort() {
        const wallet = window.verifiedWalletAddress;
        if (!wallet) return Promise.resolve(false);

        const url = new URL('/api/session/release', window.location.origin).toString();
        const headers = UI._axonosReleaseSessionHeaders();
        if (!headers) {
            return Promise.resolve(false);
        }

        const request = fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers,
            body: JSON.stringify({ wallet_address: wallet }),
        }).then((r) => {
            const ct = (r.headers.get('content-type') || '');
            if (!ct.includes('application/json')) return {};
            return r.json();
        }).then((data) => data && data.released === true)
          .catch(() => false);

        // Don't block disconnect indefinitely if the release endpoint is slow/unreachable.
        const timeout = new Promise((resolve) => {
            setTimeout(() => resolve(false), 1500);
        });
        return Promise.race([request, timeout]);
    },

    /** Cancel in-flight WebRTC negotiation and clear stale peer UI globals. */
    _axonosCancelWebRtcClient() {
        if (typeof window.axonosCancelWebRtcNegotiation === 'function') {
            try {
                window.axonosCancelWebRtcNegotiation();
            } catch (err) {
                Log.Warn('AxonOS WebRTC cancel failed: ' + err);
            }
        }
    },

    _axonosReturnToHomeAfterDisconnect(options) {
        const opts = options && typeof options === 'object' ? options : {};
        if (opts.resetWebRtc !== false) {
            UI._axonosCancelWebRtcClient();
        }
        UI.connected = false;
        if (!opts.preserveStatus) {
            UI.hideStatus();
        }
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }
        UI.updateVisualState('disconnected');
        document.title = PAGE_TITLE;
        UI.openControlbar();
        UI.openConnectPanel();
        UI.updateSessionControlButtons();
    },

    /** Server ended or released the session (heartbeat while detached or idle). */
    _axonosOnServerSessionEnded() {
        if (!window.axonosSessionDetached && UI._axgtSessionDesktopActive()) {
            UI.disconnect();
            return;
        }
        window.axonosSessionDetached = false;
        if (typeof window.axonosClearDetachedSession === 'function') {
            window.axonosClearDetachedSession();
        }
        if (UI._axgtStatusPollId) {
            clearInterval(UI._axgtStatusPollId);
            UI._axgtStatusPollId = null;
        }
        UI._axonosReturnToHomeAfterDisconnect();
        UI.showStatus(_("Session ended on the server. Launch to start a new desktop."), 'normal', 4000);
        if (typeof window.axonosRefreshPausedResumeStatus === 'function') {
            window.axonosRefreshPausedResumeStatus();
        }
    },

    _axonosCompleteDetachUI() {
        UI._axonosCancelWebRtcClient();
        UI.connected = false;
        UI.hideStatus();
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }
        UI.updateVisualState('disconnected');
        UI.showStatus(
            _("Detached — desktop still running. Launch again to reconnect, or End session when done."),
            'normal'
        );
        document.title = PAGE_TITLE;
        UI.openControlbar();
        UI.openConnectPanel();
        if (!UI._axgtStatusPollId) {
            UI._axgtStartSessionBillingPoll();
        }
        UI.updateSessionControlButtons();
        if (typeof window.axonosSyncDetachedProfileUiImmediate === 'function') {
            window.axonosSyncDetachedProfileUiImmediate();
        }
        if (typeof window.axonosOnDetachedToHome === 'function') {
            window.axonosOnDetachedToHome();
        }
    },

    _axonosCreateRfbConnection(password, includeQueryAuthToken) {
        let url;
        url = UI.getSetting('encrypt') ? 'wss' : 'ws';
        url += '://' + UI.getSetting('host');
        if (UI.getSetting('port')) {
            url += ':' + UI.getSetting('port');
        }
        url += '/' + UI.getSetting('path');

        const verifiedWallet = window.verifiedWalletAddress || null;
        const verifiedAuthToken = window.verifiedWalletAuthToken || null;
        if (verifiedWallet) {
            const sep = url.includes('?') ? '&' : '?';
            url += sep + 'wallet=' + encodeURIComponent(verifiedWallet);
            if (includeQueryAuthToken && verifiedAuthToken) {
                url += '&auth_token=' + encodeURIComponent(verifiedAuthToken);
            }
        }

        UI.rfb = new RFB(document.getElementById('noVNC_container'), url,
            { shared: UI.getSetting('shared'),
                repeaterID: UI.getSetting('repeaterID'),
                credentials: { password: password } });
        UI.rfb.addEventListener("connect", UI.connectFinished);
        UI.rfb.addEventListener("disconnect", UI.disconnectFinished);
        UI.rfb.addEventListener("credentialsrequired", UI.credentials);
        UI.rfb.addEventListener("securityfailure", UI.securityFailed);
        UI.rfb.addEventListener("capabilities", UI.updatePowerButton);
        UI.rfb.addEventListener("clipboard", UI.clipboardReceive);
        UI.rfb.addEventListener("bell", UI.bell);
        UI.rfb.addEventListener("desktopname", UI.updateDesktopName);
        UI.rfb.clipViewport = UI.getSetting('view_clip');
        UI.rfb.scaleViewport = UI.getSetting('resize') === 'scale';
        UI.rfb.resizeSession = UI.getSetting('resize') === 'remote';
        UI.rfb.qualityLevel = parseInt(UI.getSetting('quality'));
        UI.rfb.compressionLevel = parseInt(UI.getSetting('compression'));
        UI.rfb.showDotCursor = UI.getSetting('show_dot');

        UI.updateViewOnly();
    },

    connect(event, password) {

        UI.inhibitReconnect = false;

        if (UI.connected && UI._axgtSessionDesktopActive()) {
            UI.closeConnectPanel();
            UI._axgtUpdateUsageOverlay('hidden');
            if (!UI._axgtStatusPollId) {
                UI._axgtStartSessionBillingPoll();
            }
            UI.focusRemoteDesktop();
            return;
        }

        if (typeof window.axonosPrepareDesktopLaunch === 'function') {
            window.axonosPrepareDesktopLaunch();
        } else if (typeof window.axonosHideQueueOverlay === 'function') {
            window.axonosHideQueueOverlay();
        }

        // Stale RFB from a failed WebSocket (1006): disconnect may not always fire before
        // retry — hard-reset client state and recurse (short delay lets the stack unwind).
        if (typeof UI.rfb !== 'undefined' && UI.rfb) {
            if (UI._axgtSessionDesktopActive()) {
                UI.focusRemoteDesktop();
                return;
            }
            Log.Info("AxonOS: hard reset stale RFB before connect");
            const passwordArg = typeof password === 'undefined'
                ? (UI.reconnectPassword ?? WebUtil.getConfigVar('password'))
                : password;
            try {
                UI.rfb.disconnect();
            } catch (err) {
                Log.Warn("AxonOS stale RFB disconnect: " + err);
            }
            UI.rfb = undefined;
            UI.connected = false;
            setTimeout(() => UI.connect(event, passwordArg), 50);
            return;
        }

        if (UI.connected) {
            UI.connected = false;
        }

        // Read AXGT WS auth mode from URL (or cookie/local storage), without registering
        // it as a noVNC setting (no corresponding UI control exists in vnc.html).
        const wsAuthMode = String(
            WebUtil.getConfigVar('axgt_ws_auth') ??
            WebUtil.readSetting('axgt_ws_auth') ??
            'cookie'
        ).toLowerCase();
        const includeQueryAuthToken = (wsAuthMode === 'query' || wsAuthMode === 'both');

        // Check if wallet is verified before connecting
        if (!window.verifiedWalletAddress) {
            Log.Warn("Wallet not verified - showing credentials dialog");
            // Trigger credentials dialog which will show wallet verification
            UI.credentials({ detail: { types: ['password'] } });
            return;
        }
        if (includeQueryAuthToken && !window.verifiedWalletAuthToken) {
            Log.Warn("Wallet auth token missing - showing credentials dialog");
            UI.credentials({ detail: { types: ['password'] } });
            return;
        }

        const host = UI.getSetting('host');

        if (typeof password === 'undefined') {
            password = WebUtil.getConfigVar('password');
            UI.reconnectPassword = password;
        }

        if (password === null) {
            password = undefined;
        }

        UI.hideStatus();

        if (!host) {
            Log.Error("Can't connect when host is: " + host);
            UI.showStatus(_("Must set host"), 'error');
            return;
        }

        if (typeof window.showConnectionLoader === 'function') {
            window.showConnectionLoader('preparing');
        } else if (typeof window.axonosSetLaunchBusy === 'function') {
            window.axonosSetLaunchBusy(true);
        }

        // AxonOS gate rejects WebSocket upgrade unless this wallet owns the active session
        // (see websockify_gate / gate_server). Launch previously skipped claim if the user
        // left the queue and clicked connect — server returned 403 / abnormal close (1006).
        const runSessionClaim = () => {
            if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                window.axonosSetConnectionLoaderPhase('claiming');
            }
            if (window.axonosPausedResume &&
                typeof window.axonosResumeDesktopConnectIfPaused === 'function' &&
                window.axonosResumeDesktopConnectIfPaused()) {
                return;
            }
            UI._axonosFetchSessionClaim().then((claim) => {
                const granted = claim && (claim.granted === true || claim.granted === 'true');
                if (!granted) {
                    if (typeof window.axonosHideConnectionLoader === 'function') {
                        window.axonosHideConnectionLoader(true);
                    } else if (typeof window.axonosSetLaunchBusy === 'function') {
                        window.axonosSetLaunchBusy(false);
                    }
                    UI.updateVisualState('disconnected');
                    const reason = (claim && claim.reason) ? String(claim.reason) : _('Could not claim desktop session.');
                    UI.showStatus(reason, 'warn');
                    if (typeof window.axonosOnSessionClaimDenied === 'function') {
                        window.axonosOnSessionClaimDenied(claim || {});
                    }
                    return;
                }
                if (typeof window.axonosRememberOwnedSession === 'function') {
                    window.axonosRememberOwnedSession(claim);
                }
                if (claim && claim.resumed === true && typeof window.axonosRefreshPausedResumeStatus === 'function') {
                    window.axonosPausedResume = null;
                    window.axonosRefreshPausedResumeStatus();
                    UI.showStatus(_('Resumed your saved desktop session'), 'normal', 2500);
                } else if (Array.isArray(claim.assigned_gpu_ids) && claim.assigned_gpu_ids.length > 0) {
                    UI.showStatus(`Session active on GPU(s): ${claim.assigned_gpu_ids.join(',')}`, 'normal', 2500);
                } else if (claim && claim.allocation_status === 'allocating') {
                    UI.showStatus(_('Allocating GPUs...'), 'normal', 2000);
                }
                UI.closeConnectPanel();
                UI.updateVisualState('connecting');
                if (typeof window.showConnectionLoader === 'function') {
                    window.showConnectionLoader('claiming');
                }
                (async () => {
                    let usedWebRtc = false;
                    const cfgPeek = await fetch('./api/config', { credentials: 'include' })
                        .then((r) => r.json())
                        .catch(() => ({}));
                    if (cfgPeek.webrtc_enabled) {
                        if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                            window.axonosSetConnectionLoaderPhase('webrtc');
                        }
                        try {
                            const mod = await import(`./webrtc/axonos-webrtc.js?v=${Date.now()}`);
                            if (typeof mod.cancelAxonOSWebRTCNegotiation === 'function') {
                                window.axonosCancelWebRtcNegotiation = mod.cancelAxonOSWebRTCNegotiation;
                            }
                            usedWebRtc = await mod.connectAxonOSWebRTC({ UI });
                        } catch (weErr) {
                            Log.Warn('AxonOS WebRTC path failed: ' + weErr);
                        }
                        if (window.axonosWebRtcConnectAborted) {
                            if (typeof window.axonosHideConnectionLoader === 'function') {
                                window.axonosHideConnectionLoader(true);
                            }
                            UI.updateVisualState('disconnected');
                            UI.openConnectPanel();
                            return;
                        }
                        if (!usedWebRtc && cfgPeek.webrtc_fallback_enabled === false) {
                            if (typeof window.axonosHideConnectionLoader === 'function') {
                                window.axonosHideConnectionLoader(true);
                            }
                            UI.updateVisualState('disconnected');
                            UI.showStatus(_('WebRTC connection is required but failed. Check STUN/TURN or try again.'), 'error');
                            UI.openConnectPanel();
                            return;
                        }
                    }
                    if (!usedWebRtc) {
                        if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                            window.axonosSetConnectionLoaderPhase('vnc');
                        }
                        UI._axonosCreateRfbConnection(password, includeQueryAuthToken);
                    }
                })();
            }).catch((err) => {
                Log.Error('AxonOS session claim failed: ' + err);
                if (typeof window.axonosHideConnectionLoader === 'function') {
                    window.axonosHideConnectionLoader(true);
                } else if (typeof window.axonosSetLaunchBusy === 'function') {
                    window.axonosSetLaunchBusy(false);
                }
                UI.updateVisualState('disconnected');
                UI.showStatus(_('Could not claim desktop session. Check network.'), 'error');
            });
        };

        if (typeof window.axonosRefreshPausedResumeStatus === 'function') {
            window.axonosRefreshPausedResumeStatus()
                .catch(() => null)
                .finally(runSessionClaim);
        } else {
            runSessionClaim();
        }
    },

    disconnect(options) {
        const opts = options && typeof options === 'object' ? options : {};
        const skipRelease = opts.skipRelease === true;
        const detach = opts.detach === true;

        UI._axonosCancelWebRtcClient();
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }

        if (detach) {
            window.axonosSessionDetached = true;
            if (typeof window.axonosSyncDetachedProfileUiImmediate === 'function') {
                window.axonosSyncDetachedProfileUiImmediate();
            }
        } else if (!skipRelease) {
            window.axonosSessionDetached = false;
            if (typeof window.axonosClearDetachedSession === 'function') {
                window.axonosClearDetachedSession();
            }
        }

        UI.connected = false;

        if (!detach && !window.axonosSessionDetached) {
            if (UI._axgtStatusPollId) {
                clearInterval(UI._axgtStatusPollId);
                UI._axgtStatusPollId = null;
            }
        }

        // Disable automatic reconnecting
        UI.inhibitReconnect = true;

        UI.updateVisualState('disconnecting');

        // Clear any stale queue overlay/poller immediately on explicit disconnect.
        if (typeof window.axonosResetQueueClientState === 'function') {
            try {
                window.axonosResetQueueClientState();
            } catch (err) {
                Log.Warn("AxonOS queue overlay reset failed: " + err);
            }
        } else if (typeof window.axonosHideQueueOverlay === 'function') {
            try {
                window.axonosHideQueueOverlay();
            } catch (err) {
                Log.Warn("AxonOS queue overlay reset failed: " + err);
            }
        }

        const doDisconnect = () => {
            if (typeof window.axonosWebRtcTeardown === 'function') {
                Promise.resolve(window.axonosWebRtcTeardown()).finally(() => {
                    doDisconnectRfb();
                });
                return;
            }
            doDisconnectRfb();
        };

        const doDisconnectRfb = () => {
            if (UI.rfb && typeof UI.rfb.disconnect === 'function') {
                try {
                    UI.rfb.disconnect();
                } catch (err) {
                    Log.Warn("AxonOS disconnect failed: " + err);
                    if (detach || window.axonosSessionDetached) {
                        UI._axonosCompleteDetachUI();
                    } else {
                        UI._axonosReturnToHomeAfterDisconnect();
                    }
                }
            } else if (detach || window.axonosSessionDetached) {
                UI._axonosCompleteDetachUI();
            } else {
                UI._axonosReturnToHomeAfterDisconnect();
            }
        };

        // Credit exhaustion / detach must not release the session (container preserved).
        if (skipRelease) {
            doDisconnect();
            return;
        }
        // Best-effort server-side release first so reconnect doesn't get trapped behind stale ownership.
        UI._axonosReleaseSessionBestEffort().finally(doDisconnect);
    },

    /** Final heartbeat (pause session) then disconnect without killing the container. */
    _axgtDisconnectForCreditExhaustion(overlayMessage) {
        UI.inhibitReconnect = true;
        if (typeof window !== 'undefined') {
            window.axonosAllowVncConnect = false;
        }
        if (typeof window.axonosResetQueueClientState === 'function') {
            window.axonosResetQueueClientState();
        } else if (typeof window.axonosHideQueueOverlay === 'function') {
            window.axonosHideQueueOverlay();
        }
        const resumeHint = ' Your desktop is saved — add credit, then use Resume desktop session (same GPUs).';
        UI._axgtUpdateUsageOverlay(
            'locked',
            (overlayMessage || 'Usage credit exhausted. Add more ETH to unlock access.') + resumeHint
        );

        const wallet = window.verifiedWalletAddress;
        const token = window.verifiedWalletAuthToken || null;
        const finishDisconnect = () => {
            if (typeof window.axonosRefreshPausedResumeStatus === 'function') {
                window.axonosRefreshPausedResumeStatus();
            }
            setTimeout(() => UI.disconnect({ skipRelease: true }), 400);
        };

        if (!wallet) {
            finishDisconnect();
            return;
        }

        const headers = {
            'Content-Type': 'application/json',
            'X-Wallet-Address': wallet,
        };
        if (token) {
            headers['X-AXGT-Auth-Token'] = token;
        }
        fetch(new URL('/api/session/heartbeat', window.location.origin).toString(), {
            method: 'POST',
            credentials: 'include',
            headers,
            body: JSON.stringify({ wallet_address: wallet }),
        })
            .then((r) => (r.ok ? r.json() : null))
            .then((hb) => {
                if (hb && hb.paused_for_resume &&
                    typeof window.axonosApplyPausedResumeFromPayload === 'function') {
                    window.axonosApplyPausedResumeFromPayload(hb);
                }
                if (typeof window.axonosRefreshPausedResumeStatus === 'function') {
                    window.axonosRefreshPausedResumeStatus();
                }
            })
            .catch(() => {})
            .finally(finishDisconnect);
    },

    reconnect() {
        UI.reconnectCallback = null;

        // if reconnect has been disabled in the meantime, do nothing.
        if (UI.inhibitReconnect) {
            return;
        }

        UI.connect(null, UI.reconnectPassword);
    },

    cancelReconnect() {
        if (UI.reconnectCallback !== null) {
            clearTimeout(UI.reconnectCallback);
            UI.reconnectCallback = null;
        }

        UI.updateVisualState('disconnected');

        UI.openControlbar();
        UI.openConnectPanel();
    },

    connectFinished(e) {
        UI.connected = true;
        window.axonosSessionDetached = false;
        UI.inhibitReconnect = false;
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }

        let msg;
        if (UI.getSetting('encrypt')) {
            msg = _("Connected (encrypted) to ") + UI.desktopName;
        } else {
            msg = _("Connected (unencrypted) to ") + UI.desktopName;
        }
        UI.showStatus(msg);
        UI.updateVisualState('connected');
        UI.startClipboardAutoSync();

        UI._axgtStartSessionBillingPoll();
        UI.updateSessionControlButtons();

        // Do this last because it can only be used on rendered elements
        UI.focusRemoteDesktop();
    },

    disconnectFinished(e) {
        const wasConnected = UI.connected;
        const detaching = window.axonosSessionDetached === true;

        // This variable is ideally set when disconnection starts, but
        // when the disconnection isn't clean or if it is initiated by
        // the server, we need to do it here as well since
        // UI.disconnect() won't be used in those cases.
        UI._axonosCancelWebRtcClient();
        UI.connected = false;
        UI.stopClipboardAutoSync();

        UI.rfb = undefined;

        if (typeof window.axonosResetQueueClientState === 'function') {
            try {
                window.axonosResetQueueClientState();
            } catch (err) {
                Log.Warn("AxonOS queue overlay reset failed: " + err);
            }
        } else if (typeof window.axonosHideQueueOverlay === 'function') {
            try {
                window.axonosHideQueueOverlay();
            } catch (err) {
                Log.Warn("AxonOS queue overlay reset failed: " + err);
            }
        }

        if (detaching) {
            const overlay = document.getElementById('axonos_usage_overlay');
            if (!overlay || !overlay.classList.contains('axonos-usage-overlay--locked')) {
                UI._axgtUpdateUsageOverlay('hidden');
            }
            UI._axonosCompleteDetachUI();
            return;
        }

        if (UI._axgtStatusPollId) {
            clearInterval(UI._axgtStatusPollId);
            UI._axgtStatusPollId = null;
        }
        const overlay = document.getElementById('axonos_usage_overlay');
        if (!overlay || !overlay.classList.contains('axonos-usage-overlay--locked')) {
            UI._axgtUpdateUsageOverlay('hidden');
        }

        if (!e.detail.clean) {
            UI.updateVisualState('disconnected');
            if (wasConnected) {
                UI.showStatus(_("Something went wrong, connection is closed"),
                              'error');
            } else {
                UI.showStatus(_("Failed to connect to server"), 'error');
            }
        } else if (UI.getSetting('reconnect', false) === true && !UI.inhibitReconnect) {
            UI.updateVisualState('reconnecting');

            const delay = parseInt(UI.getSetting('reconnect_delay'));
            UI.reconnectCallback = setTimeout(UI.reconnect, delay);
            return;
        } else {
            UI.updateVisualState('disconnected');
            UI.showStatus(_("Session ended"), 'normal');
        }

        UI._axonosReturnToHomeAfterDisconnect({ preserveStatus: true, resetWebRtc: false });
    },

    /** True when the remote viewer is connected (RFB or WebRTC with live media). */
    _axgtSessionDesktopActive() {
        if (!UI.connected) {
            return false;
        }
        if (UI.rfb) {
            return true;
        }
        const video = document.getElementById('axonos_webrtc_video');
        if (video && video.srcObject) {
            return true;
        }
        return false;
    },

    /** True when server session should receive heartbeats (viewer or detached home). */
    _axgtSessionBillingActive() {
        if (window.axonosSessionDetached && window.verifiedWalletAddress) {
            return true;
        }
        return UI._axgtSessionDesktopActive();
    },

    /** True only on successful wallet-status when prepaid credit is actually exhausted. */
    _axgtWalletStatusCreditExhausted(httpOk, data) {
        if (!httpOk || !data || typeof data !== 'object') {
            return false;
        }
        const remaining = typeof data.remaining_minutes === 'number'
            ? data.remaining_minutes
            : null;
        if (remaining !== null && remaining > 0) {
            return false;
        }
        if (data.locked === true) {
            return true;
        }
        return data.verified === false && (remaining === null || remaining <= 0);
    },

    /** Heartbeat billing + low-credit warnings (RFB and WebRTC). */
    _axgtStartSessionBillingPoll() {
        if (!window.verifiedWalletAddress) {
            return;
        }
        UI._axgtUpdateUsageOverlay('hidden');
        if (UI._axgtStatusPollId) {
            clearInterval(UI._axgtStatusPollId);
        }
        const poll = () => UI._axgtPollWalletStatus();
        UI._axgtStatusPollId = setInterval(poll, 60000);
        setTimeout(poll, 2000);
        UI._axgtSetupUsageOverlayButton();
    },

    /** Anchor the footer countdown to an authoritative server value; ticks locally each second. */
    _axgtSetSessionTimeRemaining(wallMinutes, thresholdMinutes) {
        UI._axgtTimerAnchorSeconds = Math.max(0, wallMinutes * 60);
        UI._axgtTimerAnchorAt = Date.now();
        UI._axgtTimerThresholdSeconds = Math.max(
            0, (typeof thresholdMinutes === 'number' ? thresholdMinutes : 10) * 60
        );
        UI._axgtRenderSessionTimer();
        if (!UI._axgtTimerTickId) {
            UI._axgtTimerTickId = setInterval(() => UI._axgtRenderSessionTimer(), 1000);
        }
    },

    _axgtStopSessionTimer() {
        if (UI._axgtTimerTickId) {
            clearInterval(UI._axgtTimerTickId);
            UI._axgtTimerTickId = null;
        }
        const el = document.getElementById('axonos_session_timer');
        const sep = document.getElementById('axonos_session_timer_sep');
        if (el) el.classList.add('axonos-session-timer--hidden');
        if (sep) sep.classList.add('axonos-session-timer--hidden');
    },

    /** Render the interpolated countdown; self-stops once the session is no longer billing. */
    _axgtRenderSessionTimer() {
        const el = document.getElementById('axonos_session_timer');
        const sep = document.getElementById('axonos_session_timer_sep');
        const valEl = document.getElementById('axonos_session_timer_value');
        if (!el || !valEl) return;
        if (typeof UI._axgtSessionBillingActive === 'function' && !UI._axgtSessionBillingActive()) {
            UI._axgtStopSessionTimer();
            return;
        }
        const elapsed = (Date.now() - (UI._axgtTimerAnchorAt || Date.now())) / 1000;
        const remaining = Math.max(0, (UI._axgtTimerAnchorSeconds || 0) - elapsed);
        const mins = Math.floor(remaining / 60);
        const secs = Math.floor(remaining % 60);
        valEl.textContent = mins + ':' + (secs < 10 ? '0' : '') + secs;
        const threshold = UI._axgtTimerThresholdSeconds || 600;
        el.classList.remove('axonos-session-timer--hidden');
        if (sep) sep.classList.remove('axonos-session-timer--hidden');
        el.classList.toggle('axonos-session-timer--warning', remaining > 60 && remaining <= threshold);
        el.classList.toggle('axonos-session-timer--critical', remaining <= 60);
    },

    _axgtUpdateUsageOverlay(state, message) {
        const overlay = document.getElementById('axonos_usage_overlay');
        const msgEl = document.getElementById('axonos_usage_overlay_message');
        const btn = document.getElementById('axonos_usage_overlay_verify_btn');
        const exitBtn = document.getElementById('axonos_usage_overlay_exit_btn');
        const addCreditsBtn = document.getElementById('axonos_usage_overlay_add_credits_btn');
        if (!overlay || !msgEl) return;
        overlay.classList.remove('axonos-usage-overlay--hidden', 'axonos-usage-overlay--warning', 'axonos-usage-overlay--locked');
        if (state === 'hidden') {
            UI._axgtUsageOverlayState = 'hidden';
            overlay.classList.add('axonos-usage-overlay--hidden');
            overlay.setAttribute('aria-hidden', 'true');
            if (exitBtn) exitBtn.hidden = true;
            if (addCreditsBtn) addCreditsBtn.hidden = true;
            return;
        }
        UI._axgtUsageOverlayState = state;
        overlay.setAttribute('aria-hidden', 'false');
        msgEl.textContent = message || '';
        if (state === 'warning') {
            overlay.classList.add('axonos-usage-overlay--warning');
            if (btn) btn.textContent = 'Continue session';
            if (exitBtn) exitBtn.hidden = true;
            if (addCreditsBtn) addCreditsBtn.hidden = false;
        } else if (state === 'locked') {
            overlay.classList.add('axonos-usage-overlay--locked');
            if (btn) {
                btn.textContent = (typeof window !== 'undefined' && window.axonosPausedResume)
                    ? 'Add credit to resume'
                    : 'Add credit';
            }
            if (exitBtn) exitBtn.hidden = false;
            if (addCreditsBtn) addCreditsBtn.hidden = true;
        }
    },

    /** Leave exhausted-credit overlay and return to the launch / connect homepage. */
    _axgtUsageOverlayExitToHome() {
        UI._axgtUpdateUsageOverlay('hidden');
        const credentialsDialog = document.getElementById('noVNC_credentials_dlg');
        if (credentialsDialog) {
            credentialsDialog.classList.remove('noVNC_open');
        }
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }
        if (typeof window.axonosResetQueueClientState === 'function') {
            try {
                window.axonosResetQueueClientState();
            } catch (err) {
                Log.Warn("AxonOS queue overlay reset failed: " + err);
            }
        } else if (typeof window.axonosHideQueueOverlay === 'function') {
            try {
                window.axonosHideQueueOverlay();
            } catch (err) {
                Log.Warn('AxonOS queue overlay reset failed: ' + err);
            }
        }
        if (UI.connected || typeof window.axonosWebRtcTeardown === 'function') {
            UI.disconnect({ skipRelease: true });
            return;
        }
        UI.updateVisualState('disconnected');
        UI.openControlbar();
        UI.openConnectPanel();
    },

    _axgtPollWalletStatus() {
        if (!window.verifiedWalletAddress || !UI._axgtSessionBillingActive()) {
            return;
        }
        const wallet = window.verifiedWalletAddress;
        const token = window.verifiedWalletAuthToken || null;
        const headers = { 'X-Wallet-Address': wallet };
        if (token) headers['X-AXGT-Auth-Token'] = token;

        // Session heartbeat so the desktop session is not auto-released due to timeout
        fetch(new URL('/api/session/heartbeat', window.location.origin).toString(), {
            method: 'POST',
            credentials: 'include',
            headers: { ...headers, 'Content-Type': 'application/json' },
            body: JSON.stringify({ wallet_address: wallet })
        })
            .then((r) => (r.ok ? r.json() : null))
            .then((hb) => {
                if (hb && typeof hb.billing_gpu_count === 'number') {
                    window.axonosBillingGpuCount = hb.billing_gpu_count;
                }
                if (hb && typeof hb.gpu_billing_enabled === 'boolean') {
                    window.axonosGpuBillingEnabled = hb.gpu_billing_enabled;
                }
                if (hb && hb.ok === true && hb.requested_profile &&
                    typeof window.axonosRememberOwnedSession === 'function') {
                    window.axonosRememberOwnedSession(hb);
                    if (window.axonosSessionDetached &&
                        typeof window.axonosApplyDetachedSessionUi === 'function') {
                        window.axonosApplyDetachedSessionUi(true);
                    }
                }
                if (hb && hb.ok === false) {
                    const hbReason = String(hb.reason || '');
                    if (/credit exhausted/i.test(hbReason)) {
                        if (hb.paused_for_resume &&
                            typeof window.axonosApplyPausedResumeFromPayload === 'function') {
                            window.axonosApplyPausedResumeFromPayload(hb);
                        }
                        UI._axgtDisconnectForCreditExhaustion(
                            'Usage credit exhausted. Add more ETH to unlock access.'
                        );
                    } else if (/no active session|session ended/i.test(hbReason)) {
                        UI._axonosOnServerSessionEnded();
                    }
                }
            })
            .catch(() => {});

        const url = new URL('/api/auth/wallet-status', window.location.origin);
        url.searchParams.set('wallet_address', wallet);
        const opts = {
            method: 'GET',
            credentials: 'include',
            headers
        };
        fetch(url.toString(), opts)
            .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    return;
                }
                const remaining = typeof data.remaining_minutes === 'number' ? data.remaining_minutes : 0;
                const creditExhausted = UI._axgtWalletStatusCreditExhausted(ok, data);
                const threshold = typeof data.warning_threshold_minutes === 'number' ? data.warning_threshold_minutes : 10;
                const gpuBilling = data.gpu_billing_enabled === true || window.axonosGpuBillingEnabled === true;
                const billingGpus = gpuBilling
                    ? Math.max(1, Number(data.billing_gpu_count || window.axonosBillingGpuCount || 1))
                    : 1;
                const wallRemaining = typeof data.estimated_wall_minutes_remaining === 'number'
                    ? data.estimated_wall_minutes_remaining
                    : (gpuBilling && billingGpus > 1 ? remaining / billingGpus : remaining);
                const reason = (data.reason && String(data.reason)) || '';
                // Footer countdown — re-anchored on each poll, interpolated locally between.
                if (!creditExhausted && wallRemaining > 0) {
                    UI._axgtSetSessionTimeRemaining(wallRemaining, threshold);
                } else {
                    UI._axgtStopSessionTimer();
                }
                if (creditExhausted) {
                    UI._axgtDisconnectForCreditExhaustion(
                        'Usage credit exhausted. Add more ETH to unlock access.'
                    );
                } else if (
                    (gpuBilling && billingGpus > 1 && wallRemaining <= threshold && remaining > 0) ||
                    (!gpuBilling && remaining <= threshold && remaining > 0)
                ) {
                    const warnMsg = reason || (gpuBilling && billingGpus > 1
                        ? `About ${wallRemaining.toFixed(1)} minute(s) of desktop time left (${billingGpus} GPUs, ${billingGpus}× billing). Add more ETH to continue.`
                        : `Less than ${threshold} minutes of usage credit remaining. Add more ETH to continue.`);
                    UI._axgtUpdateUsageOverlay('warning', warnMsg);
                } else if (remaining > threshold * (gpuBilling && billingGpus > 1 ? billingGpus : 1)) {
                    UI._axgtUpdateUsageOverlay('hidden');
                }
            })
            .catch(() => {});
    },

    _axgtSetupUsageOverlayButton() {
        const btn = document.getElementById('axonos_usage_overlay_verify_btn');
        if (btn && !btn.hasAttribute('data-axgt-listener')) {
            btn.setAttribute('data-axgt-listener', 'true');
            btn.addEventListener('click', () => {
                const mode = UI._axgtUsageOverlayState || 'warning';
                if (mode === 'warning') {
                    UI._axgtUpdateUsageOverlay('hidden');
                    UI.focusRemoteDesktop();
                    return;
                }
                if (mode === 'locked') {
                    UI._axgtUpdateUsageOverlay('hidden');
                    if (typeof window.axonosOpenWalletTopUpDialog === 'function') {
                        window.axonosOpenWalletTopUpDialog(true);
                    } else {
                        UI.credentials({ detail: { types: ['password'] } });
                    }
                    return;
                }
                UI._axgtUpdateUsageOverlay('hidden');
                UI.credentials({ detail: { types: ['password'] } });
            });
        }
        const addBtn = document.getElementById('axonos_usage_overlay_add_credits_btn');
        if (addBtn && !addBtn.hasAttribute('data-axgt-listener')) {
            addBtn.setAttribute('data-axgt-listener', 'true');
            addBtn.addEventListener('click', () => {
                UI._axgtUpdateUsageOverlay('hidden');
                if (typeof window.axonosOpenWalletTopUpDialog === 'function') {
                    window.axonosOpenWalletTopUpDialog(true);
                } else {
                    UI.credentials({ detail: { types: ['password'] } });
                }
            });
        }
        const exitBtn = document.getElementById('axonos_usage_overlay_exit_btn');
        if (exitBtn && !exitBtn.hasAttribute('data-axgt-listener')) {
            exitBtn.setAttribute('data-axgt-listener', 'true');
            exitBtn.addEventListener('click', () => {
                UI._axgtUsageOverlayExitToHome();
            });
        }
    },

    securityFailed(e) {
        let msg = "";
        // On security failures we might get a string with a reason
        // directly from the server. Note that we can't control if
        // this string is translated or not.
        if ('reason' in e.detail) {
            msg = _("New connection has been rejected with reason: ") +
                e.detail.reason;
        } else {
            msg = _("New connection has been rejected");
        }
        UI.showStatus(msg, 'error');
    },

/* ------^-------
 *  /CONNECTION
 * ==============
 *   PASSWORD
 * ------v------*/

    credentials(e) {
        // If wallet already verified, auto-send the default VNC password and continue.
        // This avoids getting stuck in a loop where the server asks for credentials after
        // the websocket connection is established.
        const verifiedWallet = window.verifiedWalletAddress || null;
        const verifiedAuthToken = window.verifiedWalletAuthToken || null;
        if (verifiedWallet && verifiedAuthToken) {
            if (typeof UI._axgtSessionDesktopActive === 'function' && UI._axgtSessionDesktopActive()) {
                const credentialsDialog = document.getElementById('noVNC_credentials_dlg');
                if (credentialsDialog) {
                    credentialsDialog.classList.remove('noVNC_open');
                }
                UI._axgtUpdateUsageOverlay('hidden');
                UI.focusRemoteDesktop();
                return;
            }
            // Prefer explicit config (URL/config var). Fall back to the image default.
            // NOTE: The VNC password is not a secret in this flow; access is gated by AXGT verification.
            const password = WebUtil.getConfigVar('password') || 'axonpassword';
            UI.reconnectPassword = password;
            try {
                if (UI.rfb && typeof UI.rfb.sendCredentials === 'function') {
                    UI.rfb.sendCredentials({ username: '', password: password });
                    Log.Info("Credentials sent to VNC server (wallet already verified)");
                    UI.showStatus(_("Connecting..."), "normal");
                    return;
                }
            } catch (err) {
                Log.Warn("Failed to send credentials automatically: " + err);
                // Fall through to show UI if needed
            }
        }

        // Wallet not verified yet: show wallet verification dialog (state-driven UI)
        const usernameBlock = document.getElementById("noVNC_username_block");
        const passwordBlock = document.getElementById("noVNC_password_block");
        if (usernameBlock) usernameBlock.classList.add("noVNC_hidden");
        if (passwordBlock) passwordBlock.classList.add("noVNC_hidden");

        document.getElementById('noVNC_credentials_dlg')
            .classList.add('noVNC_open');

        Log.Warn("Wallet verification required");
        UI.showStatus(_("AXGT wallet verification required"), "warning");
    },

    setCredentials(e) {
        // Prevent actually submitting the form
        e.preventDefault();

        // Check if wallet is verified
        const verifiedWallet = window.verifiedWalletAddress;
        const verifiedAuthToken = window.verifiedWalletAuthToken || null;
        if (!verifiedWallet || !verifiedAuthToken) {
            Log.Warn("Wallet not verified yet");
            // The wallet verification will be handled by the form submit handler in vnc.html
            return;
        }

        // Wallet is verified, proceed with connection using default password
        // The password is typically from config var, otherwise image default.
        const password = WebUtil.getConfigVar('password') || 'axonpassword';
        UI.reconnectPassword = password;
        
        // Close the credentials dialog
        const credentialsDialog = document.getElementById('noVNC_credentials_dlg');
        if (credentialsDialog) {
            credentialsDialog.classList.remove('noVNC_open');
        }
        
        // If RFB connection already exists and is waiting for credentials, send them
        if (UI.rfb && typeof UI.rfb.sendCredentials === 'function') {
            try {
                UI.rfb.sendCredentials({ username: '', password: password });
                Log.Info("Credentials sent to VNC server");
            } catch (e) {
                Log.Warn("RFB not ready for credentials, initiating new connection: " + e);
                // If RFB not ready, initiate connection
                UI.connect(null, password);
            }
        } else {
            // RFB not initialized yet - initiate connection now that wallet is verified
            Log.Info("Initiating VNC connection with verified wallet");
            UI.connect(null, password);
        }
    },

/* ------^-------
 *  /PASSWORD
 * ==============
 *   FULLSCREEN
 * ------v------*/

    toggleFullscreen() {
        if (document.fullscreenElement || // alternative standard method
            document.mozFullScreenElement || // currently working methods
            document.webkitFullscreenElement ||
            document.msFullscreenElement) {
            if (document.exitFullscreen) {
                document.exitFullscreen();
            } else if (document.mozCancelFullScreen) {
                document.mozCancelFullScreen();
            } else if (document.webkitExitFullscreen) {
                document.webkitExitFullscreen();
            } else if (document.msExitFullscreen) {
                document.msExitFullscreen();
            }
        } else {
            if (document.documentElement.requestFullscreen) {
                document.documentElement.requestFullscreen();
            } else if (document.documentElement.mozRequestFullScreen) {
                document.documentElement.mozRequestFullScreen();
            } else if (document.documentElement.webkitRequestFullscreen) {
                document.documentElement.webkitRequestFullscreen(Element.ALLOW_KEYBOARD_INPUT);
            } else if (document.body.msRequestFullscreen) {
                document.body.msRequestFullscreen();
            }
        }
        UI.updateFullscreenButton();
    },

    updateFullscreenButton() {
        if (document.fullscreenElement || // alternative standard method
            document.mozFullScreenElement || // currently working methods
            document.webkitFullscreenElement ||
            document.msFullscreenElement ) {
            document.getElementById('noVNC_fullscreen_button')
                .classList.add("noVNC_selected");
        } else {
            document.getElementById('noVNC_fullscreen_button')
                .classList.remove("noVNC_selected");
        }
    },

/* ------^-------
 *  /FULLSCREEN
 * ==============
 *     RESIZE
 * ------v------*/

    // Apply remote resizing or local scaling
    applyResizeMode() {
        if (!UI.rfb) return;

        UI.rfb.scaleViewport = UI.getSetting('resize') === 'scale';
        UI.rfb.resizeSession = UI.getSetting('resize') === 'remote';
    },

/* ------^-------
 *    /RESIZE
 * ==============
 * VIEW CLIPPING
 * ------v------*/

    // Update viewport clipping property for the connection. The normal
    // case is to get the value from the setting. There are special cases
    // for when the viewport is scaled or when a touch device is used.
    updateViewClip() {
        if (!UI.rfb) return;

        const scaling = UI.getSetting('resize') === 'scale';

        if (scaling) {
            // Can't be clipping if viewport is scaled to fit
            UI.forceSetting('view_clip', false);
            UI.rfb.clipViewport  = false;
        } else if (!hasScrollbarGutter) {
            // Some platforms have scrollbars that are difficult
            // to use in our case, so we always use our own panning
            UI.forceSetting('view_clip', true);
            UI.rfb.clipViewport = true;
        } else {
            UI.enableSetting('view_clip');
            UI.rfb.clipViewport = UI.getSetting('view_clip');
        }

        // Changing the viewport may change the state of
        // the dragging button
        UI.updateViewDrag();
    },

/* ------^-------
 * /VIEW CLIPPING
 * ==============
 *    VIEWDRAG
 * ------v------*/

    toggleViewDrag() {
        if (!UI.rfb) return;

        UI.rfb.dragViewport = !UI.rfb.dragViewport;
        UI.updateViewDrag();
    },

    updateViewDrag() {
        if (!UI.connected) return;

        const viewDragButton = document.getElementById('noVNC_view_drag_button');

        if (!UI.rfb.clipViewport && UI.rfb.dragViewport) {
            // We are no longer clipping the viewport. Make sure
            // viewport drag isn't active when it can't be used.
            UI.rfb.dragViewport = false;
        }

        if (UI.rfb.dragViewport) {
            viewDragButton.classList.add("noVNC_selected");
        } else {
            viewDragButton.classList.remove("noVNC_selected");
        }

        if (UI.rfb.clipViewport) {
            viewDragButton.classList.remove("noVNC_hidden");
        } else {
            viewDragButton.classList.add("noVNC_hidden");
        }
    },

/* ------^-------
 *   /VIEWDRAG
 * ==============
 *    QUALITY
 * ------v------*/

    updateQuality() {
        if (!UI.rfb) return;

        UI.rfb.qualityLevel = parseInt(UI.getSetting('quality'));
    },

/* ------^-------
 *   /QUALITY
 * ==============
 *  COMPRESSION
 * ------v------*/

    updateCompression() {
        if (!UI.rfb) return;

        UI.rfb.compressionLevel = parseInt(UI.getSetting('compression'));
    },

/* ------^-------
 *  /COMPRESSION
 * ==============
 *    KEYBOARD
 * ------v------*/

    showVirtualKeyboard() {
        if (!isTouchDevice) return;

        const input = document.getElementById('noVNC_keyboardinput');

        if (document.activeElement == input) return;

        input.focus();

        try {
            const l = input.value.length;
            // Move the caret to the end
            input.setSelectionRange(l, l);
        } catch (err) {
            // setSelectionRange is undefined in Google Chrome
        }
    },

    hideVirtualKeyboard() {
        if (!isTouchDevice) return;

        const input = document.getElementById('noVNC_keyboardinput');

        if (document.activeElement != input) return;

        input.blur();
    },

    toggleVirtualKeyboard() {
        if (document.getElementById('noVNC_keyboard_button')
            .classList.contains("noVNC_selected")) {
            UI.hideVirtualKeyboard();
        } else {
            UI.showVirtualKeyboard();
        }
    },

    onfocusVirtualKeyboard(event) {
        document.getElementById('noVNC_keyboard_button')
            .classList.add("noVNC_selected");
        if (UI.rfb) {
            UI.rfb.focusOnClick = false;
        }
    },

    onblurVirtualKeyboard(event) {
        document.getElementById('noVNC_keyboard_button')
            .classList.remove("noVNC_selected");
        if (UI.rfb) {
            UI.rfb.focusOnClick = true;
        }
    },

    keepVirtualKeyboard(event) {
        const input = document.getElementById('noVNC_keyboardinput');

        // Only prevent focus change if the virtual keyboard is active
        if (document.activeElement != input) {
            return;
        }

        // Only allow focus to move to other elements that need
        // focus to function properly
        if (event.target.form !== undefined) {
            switch (event.target.type) {
                case 'text':
                case 'email':
                case 'search':
                case 'password':
                case 'tel':
                case 'url':
                case 'textarea':
                case 'select-one':
                case 'select-multiple':
                    return;
            }
        }

        event.preventDefault();
    },

    keyboardinputReset() {
        const kbi = document.getElementById('noVNC_keyboardinput');
        kbi.value = new Array(UI.defaultKeyboardinputLen).join("_");
        UI.lastKeyboardinput = kbi.value;
    },

    keyEvent(keysym, code, down) {
        if (!UI.rfb) return;

        UI.rfb.sendKey(keysym, code, down);
    },

    // When normal keyboard events are left uncought, use the input events from
    // the keyboardinput element instead and generate the corresponding key events.
    // This code is required since some browsers on Android are inconsistent in
    // sending keyCodes in the normal keyboard events when using on screen keyboards.
    keyInput(event) {

        if (!UI.rfb) return;

        const newValue = event.target.value;

        if (!UI.lastKeyboardinput) {
            UI.keyboardinputReset();
        }
        const oldValue = UI.lastKeyboardinput;

        let newLen;
        try {
            // Try to check caret position since whitespace at the end
            // will not be considered by value.length in some browsers
            newLen = Math.max(event.target.selectionStart, newValue.length);
        } catch (err) {
            // selectionStart is undefined in Google Chrome
            newLen = newValue.length;
        }
        const oldLen = oldValue.length;

        let inputs = newLen - oldLen;
        let backspaces = inputs < 0 ? -inputs : 0;

        // Compare the old string with the new to account for
        // text-corrections or other input that modify existing text
        for (let i = 0; i < Math.min(oldLen, newLen); i++) {
            if (newValue.charAt(i) != oldValue.charAt(i)) {
                inputs = newLen - i;
                backspaces = oldLen - i;
                break;
            }
        }

        // Send the key events
        for (let i = 0; i < backspaces; i++) {
            UI.rfb.sendKey(KeyTable.XK_BackSpace, "Backspace");
        }
        for (let i = newLen - inputs; i < newLen; i++) {
            UI.rfb.sendKey(keysyms.lookup(newValue.charCodeAt(i)));
        }

        // Control the text content length in the keyboardinput element
        if (newLen > 2 * UI.defaultKeyboardinputLen) {
            UI.keyboardinputReset();
        } else if (newLen < 1) {
            // There always have to be some text in the keyboardinput
            // element with which backspace can interact.
            UI.keyboardinputReset();
            // This sometimes causes the keyboard to disappear for a second
            // but it is required for the android keyboard to recognize that
            // text has been added to the field
            event.target.blur();
            // This has to be ran outside of the input handler in order to work
            setTimeout(event.target.focus.bind(event.target), 0);
        } else {
            UI.lastKeyboardinput = newValue;
        }
    },

/* ------^-------
 *   /KEYBOARD
 * ==============
 *   EXTRA KEYS
 * ------v------*/

    openExtraKeys() {
        UI.closeAllPanels();
        UI.releaseWebRtcPointerState();
        UI.openControlbar();

        document.getElementById('noVNC_modifiers')
            .classList.add("noVNC_open");
        document.getElementById('noVNC_toggle_extra_keys_button')
            .classList.add("noVNC_selected");
    },

    closeExtraKeys() {
        document.getElementById('noVNC_modifiers')
            .classList.remove("noVNC_open");
        document.getElementById('noVNC_toggle_extra_keys_button')
            .classList.remove("noVNC_selected");
    },

    toggleExtraKeys() {
        if (document.getElementById('noVNC_modifiers')
            .classList.contains("noVNC_open")) {
            UI.closeExtraKeys();
        } else  {
            UI.openExtraKeys();
        }
    },

    sendEsc() {
        UI.sendKey(KeyTable.XK_Escape, "Escape");
    },

    sendTab() {
        UI.sendKey(KeyTable.XK_Tab, "Tab");
    },

    toggleCtrl() {
        const btn = document.getElementById('noVNC_toggle_ctrl_button');
        if (btn.classList.contains("noVNC_selected")) {
            UI.sendKey(KeyTable.XK_Control_L, "ControlLeft", false);
            btn.classList.remove("noVNC_selected");
        } else {
            UI.sendKey(KeyTable.XK_Control_L, "ControlLeft", true);
            btn.classList.add("noVNC_selected");
        }
    },

    toggleWindows() {
        const btn = document.getElementById('noVNC_toggle_windows_button');
        if (btn.classList.contains("noVNC_selected")) {
            UI.sendKey(KeyTable.XK_Super_L, "MetaLeft", false);
            btn.classList.remove("noVNC_selected");
        } else {
            UI.sendKey(KeyTable.XK_Super_L, "MetaLeft", true);
            btn.classList.add("noVNC_selected");
        }
    },

    toggleAlt() {
        const btn = document.getElementById('noVNC_toggle_alt_button');
        if (btn.classList.contains("noVNC_selected")) {
            UI.sendKey(KeyTable.XK_Alt_L, "AltLeft", false);
            btn.classList.remove("noVNC_selected");
        } else {
            UI.sendKey(KeyTable.XK_Alt_L, "AltLeft", true);
            btn.classList.add("noVNC_selected");
        }
    },

    sendCtrlAltDel() {
        if (!UI.rfb || typeof UI.rfb.sendCtrlAltDel !== 'function') {
            return;
        }
        UI.rfb.sendCtrlAltDel();
        // See below
        UI.focusRemoteDesktop();
        UI.idleControlbar();
    },

    sendKey(keysym, code, down) {
        if (!UI.rfb || typeof UI.rfb.sendKey !== 'function') {
            return;
        }
        UI.rfb.sendKey(keysym, code, down);

        // Move focus to the screen in order to be able to use the
        // keyboard right after these extra keys.
        // The exception is when a virtual keyboard is used, because
        // if we focus the screen the virtual keyboard would be closed.
        // In this case we focus our special virtual keyboard input
        // element instead.
        if (document.getElementById('noVNC_keyboard_button')
            .classList.contains("noVNC_selected")) {
            document.getElementById('noVNC_keyboardinput').focus();
        } else {
            UI.focusRemoteDesktop();
        }
        // fade out the controlbar to highlight that
        // the focus has been moved to the screen
        UI.idleControlbar();
    },

/* ------^-------
 *   /EXTRA KEYS
 * ==============
 *     MISC
 * ------v------*/

    updateViewOnly() {
        if (!UI.rfb) return;
        UI.rfb.viewOnly = UI.getSetting('view_only');

        // Hide input related buttons in view only mode
        if (UI.rfb.viewOnly) {
            document.getElementById('noVNC_keyboard_button')
                .classList.add('noVNC_hidden');
            document.getElementById('noVNC_toggle_extra_keys_button')
                .classList.add('noVNC_hidden');
            document.getElementById('noVNC_clipboard_button')
                .classList.add('noVNC_hidden');
        } else {
            document.getElementById('noVNC_keyboard_button')
                .classList.remove('noVNC_hidden');
            document.getElementById('noVNC_toggle_extra_keys_button')
                .classList.remove('noVNC_hidden');
            document.getElementById('noVNC_clipboard_button')
                .classList.remove('noVNC_hidden');
        }
    },

    updateShowDotCursor() {
        if (!UI.rfb) return;
        UI.rfb.showDotCursor = UI.getSetting('show_dot');
    },

    updateLogging() {
        if (typeof WebUtil.initLogging === 'function') {
            WebUtil.initLogging(UI.getSetting('logging'));
        }
    },

    updateDesktopName(e) {
        UI.desktopName = e.detail.name;
        // Display the desktop name in the document title
        document.title = e.detail.name + " - " + PAGE_TITLE;
    },

    bell(e) {
        if (WebUtil.getConfigVar('bell', 'on') === 'on') {
            const promise = document.getElementById('noVNC_bell').play();
            // The standards disagree on the return value here
            if (promise) {
                promise.catch((e) => {
                    if (e.name === "NotAllowedError") {
                        // Ignore when the browser doesn't let us play audio.
                        // It is common that the browsers require audio to be
                        // initiated from a user action.
                    } else {
                        Log.Error("Unable to play bell: " + e);
                    }
                });
            }
        }
    },

    //Helper to add options to dropdown.
    addOption(selectbox, text, value) {
        const optn = document.createElement("OPTION");
        optn.text = text;
        optn.value = value;
        selectbox.options.add(optn);
    },

/* ------^-------
 *    /MISC
 * ==============
 */
};

// Set up translations
const LINGUAS = ["cs", "de", "el", "es", "fr", "ja", "ko", "nl", "pl", "pt_BR", "ru", "sv", "tr", "zh_CN", "zh_TW"];
l10n.setup(LINGUAS);
if (l10n.language === "en" || l10n.dictionary !== undefined) {
    UI.prime();
} else {
    fetch('app/locale/' + l10n.language + '.json')
        .then((response) => {
            if (!response.ok) {
                throw Error("" + response.status + " " + response.statusText);
            }
            return response.json();
        })
        .then((translations) => { l10n.dictionary = translations; })
        .catch(err => Log.Error("Failed to load translations: " + err))
        .then(UI.prime);
}

export default UI;

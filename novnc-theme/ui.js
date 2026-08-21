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

const AXONOS_TEMPLATES = [
    {
        id: 'pytorch',
        title: 'PyTorch AI Lab',
        category: 'ai-ml',
        tags: ['AI/ML', 'Deep Learning'],
        desc: 'Interactive PyTorch workspace for deep learning. Includes JupyterLab, torchvision, torchaudio, and common ML development tools pre-configured for GPU acceleration.',
        image: 'axonos:public-beta',
        verifyCmd: "python3 -c 'import torch; print(f\"PyTorch {torch.__version__} GPU active:\", torch.cuda.is_available())'",
        packages: ['PyTorch 2.5+', 'CUDA 12.1', 'JupyterLab', 'TensorBoard', 'NumPy', 'Pandas'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96-.44 2.5 2.5 0 0 1 0-3.12 3 3 0 0 1 0-4.88 2.5 2.5 0 0 1 0-3.12A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96-.44 2.5 2.5 0 0 0 0-3.12 3 3 0 0 0 0-4.88 2.5 2.5 0 0 0 0-3.12A2.5 2.5 0 0 0 14.5 2Z"/></svg>'
    },
    {
        id: 'gromacs',
        title: 'GROMACS Molecular Dynamics',
        category: 'bio-chem',
        tags: ['Bio/Chem', 'Simulation'],
        desc: 'Versatile package to perform molecular dynamics, i.e. simulate the Newtonian equations of motion for systems with hundreds to millions of particles. GPU-accelerated.',
        image: 'axonos:public-beta',
        verifyCmd: "gmx -version | grep -i 'gromacs version'",
        packages: ['GROMACS 2026', 'CUDA Accelerated', 'MPI Support', 'cuFFTMp (Multi-GPU FFT)', 'NVSHMEM Support', 'PyMOL (Structure Viewer)'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><line x1="3" y1="20" x2="9" y2="14"/><line x1="21" y1="20" x2="15" y2="14"/><line x1="12" y1="3" x2="12" y2="9"/><circle cx="3" cy="20" r="2"/><circle cx="21" cy="20" r="2"/><circle cx="12" cy="3" r="2"/></svg>'
    },
    {
        id: 'ugene',
        title: 'UGENE Bioinformatics',
        category: 'bio-chem',
        tags: ['Bio/Chem', 'Genomics'],
        desc: 'Integrated bioinformatics suite. Offers a graphical interface for DNA/protein sequence analysis, alignments, phylogenetics, and secondary structure prediction.',
        image: 'axonos:public-beta',
        verifyCmd: "ugenecl --task help | head -n 1",
        packages: ['UGENE 52.1', 'UGENE CLI (ugenecl)', 'PyMOL (Structure Viewer)'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 10.5C4.5 5.5 19.5 5.5 19.5 10.5C19.5 15.5 4.5 15.5 4.5 20.5"/><path d="M19.5 10.5C19.5 5.5 4.5 5.5 4.5 10.5C4.5 15.5 19.5 15.5 19.5 20.5"/><line x1="6" y1="8" x2="18" y2="8"/><line x1="6" y1="18" x2="18" y2="18"/><line x1="12" y1="5.5" x2="12" y2="15.5"/></svg>'
    },
    {
        id: 'quantum-espresso',
        title: 'Quantum ESPRESSO',
        category: 'physics-quantum',
        tags: ['Physics', 'Quantum'],
        desc: 'An integrated suite of Open-Source computer codes for electronic-structure calculations and materials modeling at the nanoscale, based on DFT, plane waves, and pseudopotentials.',
        image: 'axonos:public-beta',
        verifyCmd: "echo | pw.x 2>&1 | grep -i 'Program PWSCF'",
        packages: ['Quantum ESPRESSO (DFT)', 'OpenMPI Support', 'XCrySDen (Visualizer)', 'Gnuplot'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="12" rx="3" ry="9" transform="rotate(45 12 12)"/><ellipse cx="12" cy="12" rx="3" ry="9" transform="rotate(-45 12 12)"/><circle cx="12" cy="12" r="2"/></svg>'
    },
    {
        id: 'rstudio',
        title: 'RStudio Data Science',
        category: 'data-science',
        tags: ['Data Science', 'Statistics'],
        desc: 'Comprehensive R workspace for statistical computing and visualization. Includes the RStudio Desktop IDE and the base R runtime environment.',
        image: 'axonos:public-beta',
        verifyCmd: "R --version | head -n 1",
        packages: ['R Environment', 'RStudio Desktop'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>'
    },
    {
        id: 'beakerx',
        title: 'BeakerX Jupyter Lab',
        category: 'data-science',
        tags: ['Data Science', 'Polyglot'],
        desc: 'An extension to Jupyter Notebook and JupyterLab providing interactive widgets, table enhancements, and JVM kernel support.',
        image: 'axonos:public-beta',
        verifyCmd: "jupyter kernelspec list",
        packages: ['JupyterLab', 'BeakerX Extension', 'BeakerX Widgets', 'Java Runtime (JRE 17)'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 3h15"/><path d="M6 3v16a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V3"/><path d="M6 14h12"/></svg>'
    },
    {
        id: 'spyder',
        title: 'Spyder Python IDE',
        category: 'data-science',
        tags: ['Data Science', 'Python', 'IDE'],
        desc: 'Scientific Python Development Environment. Powerful interactive development environment for Python with advanced editing, interactive testing, debugging, and introspection features.',
        image: 'axonos:public-beta',
        verifyCmd: "spyder --version 2>/dev/null || pip show spyder",
        packages: ['Spyder IDE', 'PyQt5', 'NumPy', 'Matplotlib'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="12" y1="2" x2="12" y2="22"/></svg>'
    },
    {
        id: 'octave',
        title: 'GNU Octave',
        category: 'data-science',
        tags: ['Data Science', 'Simulation', 'Math'],
        desc: 'Scientific programming language for numerical computations. Highly MATLAB-compatible environment for solving linear and nonlinear problems numerically.',
        image: 'axonos:public-beta',
        verifyCmd: "octave --version | head -n 1",
        packages: ['GNU Octave Runtime', 'GNU Octave GUI'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/></svg>'
    },
    {
        id: 'fiji',
        title: 'Fiji (ImageJ) Microscopy',
        category: 'bio-chem',
        tags: ['Bio/Chem', 'Imaging', 'Analysis'],
        desc: 'Image processing package for scientific microscopy. Bundles ImageJ with a curated set of plugins for biological-image analysis, registration, and segmentation.',
        image: 'axonos:public-beta',
        verifyCmd: "test -d /opt/Fiji.app",
        packages: ['Fiji (ImageJ)', 'Bio-Formats Plugin', 'Java Runtime'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>'
    },
    {
        id: 'nextflow',
        title: 'Nextflow Workflow',
        category: 'bio-chem',
        tags: ['Bio/Chem', 'Workflow', 'Bioinformatics'],
        desc: 'Workflow manager to design and run scalable, portable, and reproducible scientific pipelines using software containers (Docker, Singularity).',
        image: 'axonos:public-beta',
        verifyCmd: "nextflow -version | head -n 1",
        packages: ['Nextflow', 'Java 17 Runtime'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>'
    },
    {
        id: 'qgis-grass',
        title: 'QGIS & GRASS GIS',
        category: 'data-science',
        tags: ['Data Science', 'GIS', 'Mapping'],
        desc: 'Geographic Information System (GIS) application for geospatial data management, visual mapping, and advanced spatial raster/vector analysis.',
        image: 'axonos:public-beta',
        verifyCmd: "qgis --version | head -n 1",
        packages: ['QGIS Desktop', 'GRASS GIS 8', 'Geospatial Libraries'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>'
    },
    {
        id: 'syncthing',
        title: 'Syncthing Service',
        category: 'data-science',
        tags: ['Data Science', 'Utility', 'Sync'],
        desc: 'Continuous decentralized file synchronization program. Synchronizes files in real-time between computers, fully encrypted and peer-to-peer.',
        image: 'axonos:public-beta',
        verifyCmd: "syncthing --version | head -n 1",
        packages: ['Syncthing Daemon', 'Syncthing WebUI'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>'
    },
    {
        id: 'ethercalc',
        title: 'EtherCalc Spreadsheet',
        category: 'data-science',
        tags: ['Data Science', 'Office', 'Spreadsheet'],
        desc: 'Web-based collaborative spreadsheet. Multi-user real-time editing spreadsheet that runs in the browser.',
        image: 'axonos:public-beta',
        verifyCmd: "test -f /usr/share/applications/ethercalc.desktop",
        packages: ['EtherCalc (Browser-based)'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/></svg>'
    },
    {
        id: 'ngl-viewer',
        title: 'NGL Molecular Viewer',
        category: 'bio-chem',
        tags: ['Bio/Chem', 'Visualization'],
        desc: 'Web-based 3D molecular visualization client. Render large macromolecules, chemical structures, and simulation trajectories directly in the browser.',
        image: 'axonos:public-beta',
        verifyCmd: "test -f /usr/share/applications/nglviewer.desktop",
        packages: ['NGL Viewer (Browser-based)'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>'
    },
    {
        id: 'remix-ide',
        title: 'Remix Ethereum IDE',
        category: 'data-science',
        tags: ['Data Science', 'Web3', 'Development'],
        desc: 'Web-based Ethereum Smart Contract IDE. Develop, compile, debug, deploy, and interact with Solidity smart contracts.',
        image: 'axonos:public-beta',
        verifyCmd: "test -f /usr/share/applications/remix-ide.desktop",
        packages: ['Remix IDE (Browser-based)', 'Solidity Compiler'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5"/><polygon points="12 22 12 2"/></svg>'
    },
    {
        id: 'cellmodeller',
        title: 'CellModeller Simulation',
        category: 'bio-chem',
        tags: ['Bio/Chem', 'Simulation', 'Biophysics'],
        desc: 'Multicellular biophysical simulation framework. Computes growing bacterial cell populations, physical interactions, genetic circuits, and nutrient diffusion.',
        image: 'axonos:public-beta',
        verifyCmd: "test -d /opt/CellModeller",
        packages: ['CellModeller Engine', 'CellModeller GUI', 'OpenCL Support'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><circle cx="18" cy="6" r="2"/><circle cx="6" cy="18" r="2"/></svg>'
    },
    {
        id: 'ipfs-desktop',
        title: 'IPFS Desktop',
        category: 'data-science',
        tags: ['Data Science', 'Web3', 'Storage'],
        desc: 'InterPlanetary File System (IPFS) desktop client. Run a local IPFS peer, manage files on the decentralized network, and share storage across the Web3 ecosystem.',
        image: 'axonos:public-beta',
        verifyCmd: "which ipfs-desktop || test -f /usr/share/applications/ipfs-desktop.desktop",
        packages: ['IPFS Desktop GUI', 'IPFS Daemon (kubo)', 'IPFS CLI (ipfs)'],
        icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4ec3d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>'
    }
];

// Expose the catalog to the inline vnc.html scripts. ui.js loads as a module,
// so AXONOS_TEMPLATES is otherwise module-private and the landing/wizard summary
// code (which runs in global scope) can't read it.
window.AXONOS_TEMPLATES = AXONOS_TEMPLATES;

const UI = {

    connected: false,
    /** Active viewer transport. `connected` remains the upstream RFB/WebRTC flag. */
    connectionKind: null,
    terminalClient: null,
    terminalState: 'idle',
    _axonosTerminalOpenGeneration: 0,
    _axonosTerminalOpenAbort: null,
    _axonosPendingTerminalClaim: null,
    _axonosSshClaim: null,
    desktopName: "",

    statusTimeout: null,
    // Minimum on-screen dwell for error banners, in ms. Long enough to read a
    // full sentence, short enough that a transient failure clears itself.
    STATUS_ERROR_TIMEOUT: 8000,
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
    /** Monotonic identity for the active Launch/Resume connection pipeline. */
    _axonosConnectGeneration: 0,
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
    /** Structured reason from the last unsuccessful WebRTC connect attempt. */
    webrtcLastFailure: null,

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
        UI.initAxonosTemplates();
        UI.initAxonosSshToggle();
        UI.addConnectionControlHandlers();
        UI.addClipboardHandlers();
        UI.addFilesHandlers();
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

    /** Tab close on a desktop viewer → release; F5/Ctrl+R → keep session
     *  (reload); detached and SSH/web-terminal sessions survive tab close. */
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

    persistAxonosSelectedTemplate() {
        // Persist across the End-session → page-reload cycle (same tab) so a launch
        // after reload still carries the template. Templates apply only at spawn.
        try {
            if (window.axonosSelectedTemplateId) {
                window.sessionStorage.setItem('axonosSelectedTemplateId', window.axonosSelectedTemplateId);
            } else {
                window.sessionStorage.removeItem('axonosSelectedTemplateId');
            }
        } catch (e) { /* sessionStorage unavailable; selection just won't persist */ }
    },

    initAxonosTemplates() {
        // Restore the last selection (survives reload); ignore unknown ids.
        let restored = null;
        try { restored = window.sessionStorage.getItem('axonosSelectedTemplateId'); } catch (e) { restored = null; }
        if (restored && !AXONOS_TEMPLATES.some(t => t.id === restored)) {
            restored = null;
        }
        window.axonosSelectedTemplateId = restored;

        // Bind both landing browse entry points to a read-only catalog. Browsing
        // templates must never require or initiate wallet authentication.
        const viewAllBtn = document.getElementById('axonos_landing_view_all_btn');
        const viewAllCount = document.getElementById('axonos_landing_view_all_count');
        if (viewAllCount && typeof AXONOS_TEMPLATES !== 'undefined') {
            viewAllCount.textContent = AXONOS_TEMPLATES.length;
        }
        if (viewAllBtn) {
            viewAllBtn.addEventListener('click', UI.openAxonosCatalogModal);
        }

        // Same catalog entry point from the workspace's Quick Launch header.
        const dashViewAllBtn = document.getElementById('axonos_dashboard_view_all_btn');
        const dashViewAllCount = document.getElementById('axonos_dashboard_view_all_count');
        if (dashViewAllCount && typeof AXONOS_TEMPLATES !== 'undefined') {
            dashViewAllCount.textContent = AXONOS_TEMPLATES.length;
        }
        if (dashViewAllBtn) {
            dashViewAllBtn.addEventListener('click', UI.openAxonosCatalogModal);
        }

        const catalogModal = document.getElementById('axonos_catalog_modal');
        const catalogClose = document.getElementById('axonos_catalog_modal_close');
        const catalogOverlay = document.getElementById('axonos_catalog_modal_overlay');
        const closeCatalog = () => {
            if (!catalogModal) return;
            catalogModal.classList.remove('active');
            catalogModal.setAttribute('aria-hidden', 'true');
        };
        if (catalogClose) catalogClose.addEventListener('click', closeCatalog);
        if (catalogOverlay) catalogOverlay.addEventListener('click', closeCatalog);
        window.axonosOpenCatalogModal = UI.openAxonosCatalogModal;

        // Close Modal Event Listeners
        const modal = document.getElementById('axonos_template_modal');
        const modalClose = document.getElementById('axonos_modal_close');
        const modalOverlay = document.getElementById('axonos_modal_overlay');

        const closeModal = () => {
            if (modal) {
                modal.classList.remove('active');
                modal.setAttribute('aria-hidden', 'true');
            }
        };

        if (modalClose) {
            modalClose.addEventListener('click', closeModal);
        }
        if (modalOverlay) {
            modalOverlay.addEventListener('click', closeModal);
        }
        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && catalogModal && catalogModal.classList.contains('active')) {
                closeCatalog();
                return;
            }
            if (e.key === 'Escape' && modal && modal.classList.contains('active')) {
                closeModal();
            }
        });

        // Initial Render
        UI.renderLandingFeaturedTemplates();
        UI.renderDashboardQuickLaunch();
        UI.updateAxonosSelectedTemplateBanner();
    },

    openAxonosCatalogModal() {
        const modal = document.getElementById('axonos_catalog_modal');
        const grid = document.getElementById('axonos_catalog_modal_grid');
        if (!modal || !grid) return;

        grid.replaceChildren();
        const parser = new DOMParser();
        AXONOS_TEMPLATES.forEach((template) => {
            const card = document.createElement('article');
            card.className = 'axonos-catalog-card';

            const icon = document.createElement('div');
            icon.className = 'axonos-template-icon-wrap';
            try {
                icon.appendChild(parser.parseFromString(template.icon, 'image/svg+xml').documentElement);
            } catch (err) {
                icon.textContent = '🧬';
            }

            const copy = document.createElement('div');
            const title = document.createElement('h3');
            title.textContent = template.title;
            const tags = document.createElement('p');
            tags.textContent = template.tags.join(' · ');
            const desc = document.createElement('p');
            desc.className = 'axonos-catalog-card-desc';
            desc.textContent = template.desc;
            copy.append(title, tags, desc);

            const details = document.createElement('button');
            details.type = 'button';
            details.className = 'axonos-template-btn info-btn';
            details.textContent = 'View details';
            details.addEventListener('click', () => UI.showAxonosTemplateDetails(template));

            card.append(icon, copy, details);
            grid.appendChild(card);
        });

        modal.classList.add('active');
        modal.setAttribute('aria-hidden', 'false');
        const close = document.getElementById('axonos_catalog_modal_close');
        if (close) close.focus();
    },

    renderLandingFeaturedTemplates() {
        const grid = document.getElementById('axonos_landing_featured_grid');
        if (!grid) return;
        grid.replaceChildren();

        const parser = new DOMParser();
        const featured = AXONOS_TEMPLATES.slice(0, 4);

        featured.forEach(t => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.style.cssText = "text-align:left;cursor:pointer;border-radius:18px;border:1px solid rgba(255,255,255,.07);background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012));padding:20px;display:flex;flex-direction:column;gap:14px;min-height:172px;width:100%;transition: all 0.2s ease-in-out;outline:none;";
            
            btn.addEventListener('mouseenter', () => {
                btn.style.borderColor = 'rgba(123,108,255,.4)';
                btn.style.background = 'linear-gradient(180deg,rgba(123,108,255,.08),rgba(255,255,255,.012))';
                btn.style.transform = 'translateY(-2px)';
            });
            btn.addEventListener('mouseleave', () => {
                btn.style.borderColor = 'rgba(255,255,255,.07)';
                btn.style.background = 'linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012))';
                btn.style.transform = 'translateY(0)';
            });

            const headerRow = document.createElement('div');
            headerRow.style.cssText = "display:flex;align-items:center;justify-content:space-between;width:100%;";

            const iconWrap = document.createElement('div');
            iconWrap.style.cssText = "width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);";
            try {
                const parsedSvg = parser.parseFromString(t.icon, 'image/svg+xml');
                iconWrap.appendChild(parsedSvg.documentElement);
            } catch (err) {
                iconWrap.textContent = '🧬';
            }

            const catTag = document.createElement('span');
            catTag.style.cssText = "font-size:10px;font-weight:600;letter-spacing:.05em;color:#9AA0BA;padding:4px 9px;border-radius:7px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);";
            const catLabels = { 'ai-ml': 'AI/ML', 'bio-chem': 'Bio/Chem', 'data-science': 'Data Science', 'physics-quantum': 'Physics/Quantum' };
            catTag.textContent = catLabels[t.category] || t.category;

            headerRow.appendChild(iconWrap);
            headerRow.appendChild(catTag);

            const textWrap = document.createElement('div');
            textWrap.style.cssText = "display:flex;flex-direction:column;gap:6px;";

            const title = document.createElement('div');
            title.style.cssText = "font-size:15.5px;font-weight:600;color:#F1F2F8;letter-spacing:-.01em;";
            title.textContent = t.title;

            const shortDesc = document.createElement('div');
            shortDesc.style.cssText = "font-size:12.5px;line-height:1.5;color:#878DA6;font-weight:300;";
            shortDesc.textContent = t.desc.split('.')[0] + '.';

            textWrap.appendChild(title);
            textWrap.appendChild(shortDesc);

            btn.appendChild(headerRow);
            btn.appendChild(textWrap);

            btn.addEventListener('click', () => {
                UI.showAxonosTemplateDetails(t);
            });

            grid.appendChild(btn);
        });
    },

    renderDashboardQuickLaunch() {
        const grid = document.getElementById('axonos_dashboard_quick_launch');
        if (!grid) return;
        grid.replaceChildren();

        const parser = new DOMParser();
        const featured = AXONOS_TEMPLATES.slice(0, 4);

        featured.forEach(t => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.style.cssText = "text-align:left;cursor:pointer;border-radius:18px;border:1px solid rgba(255,255,255,.07);background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012));padding:20px;display:flex;flex-direction:column;gap:14px;min-height:160px;width:100%;transition: all 0.2s ease-in-out;outline:none;";
            
            btn.addEventListener('mouseenter', () => {
                btn.style.borderColor = 'rgba(123,108,255,.4)';
                btn.style.background = 'linear-gradient(180deg,rgba(123,108,255,.08),rgba(255,255,255,.012))';
                btn.style.transform = 'translateY(-2px)';
            });
            btn.addEventListener('mouseleave', () => {
                btn.style.borderColor = 'rgba(255,255,255,.07)';
                btn.style.background = 'linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012))';
                btn.style.transform = 'translateY(0)';
            });

            const headerRow = document.createElement('div');
            headerRow.style.cssText = "display:flex;align-items:center;justify-content:space-between;width:100%;";

            const iconWrap = document.createElement('div');
            iconWrap.style.cssText = "width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);";
            try {
                const parsedSvg = parser.parseFromString(t.icon, 'image/svg+xml');
                iconWrap.appendChild(parsedSvg.documentElement);
            } catch (err) {
                iconWrap.textContent = '🧬';
            }

            const catTag = document.createElement('span');
            catTag.style.cssText = "font-size:10px;font-weight:600;letter-spacing:.05em;color:#9AA0BA;padding:4px 9px;border-radius:7px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);";
            const catLabels = { 'ai-ml': 'AI/ML', 'bio-chem': 'Bio/Chem', 'data-science': 'Data Science', 'physics-quantum': 'Physics/Quantum' };
            catTag.textContent = catLabels[t.category] || t.category;

            headerRow.appendChild(iconWrap);
            headerRow.appendChild(catTag);

            const textWrap = document.createElement('div');
            textWrap.style.cssText = "display:flex;flex-direction:column;gap:6px;";

            const title = document.createElement('div');
            title.style.cssText = "font-size:15.5px;font-weight:600;color:#F1F2F8;letter-spacing:-.01em;";
            title.textContent = t.title;

            const shortDesc = document.createElement('div');
            shortDesc.style.cssText = "font-size:12.5px;line-height:1.5;color:#878DA6;font-weight:300;";
            shortDesc.textContent = t.desc.split('.')[0] + '.';

            textWrap.appendChild(title);
            textWrap.appendChild(shortDesc);

            btn.appendChild(headerRow);
            btn.appendChild(textWrap);

            btn.addEventListener('click', () => {
                window.axonosSelectedTemplateId = t.id;
                UI.persistAxonosSelectedTemplate();
                UI.updateAxonosSelectedTemplateBanner();
                if (typeof axonosStartWizard === 'function') {
                    axonosStartWizard();
                }
            });

            grid.appendChild(btn);
        });
    },

    renderAxonosTemplates(searchTerm = '', activeCategory = 'all', gridSelector = '#axonos_wizard_templates_grid') {
        const grid = document.querySelector(gridSelector);
        if (!grid) return;
        grid.replaceChildren();

        const filtered = AXONOS_TEMPLATES.filter(t => {
            const matchesCategory = (activeCategory === 'all' || t.category === activeCategory);
            const matchesSearch = !searchTerm || 
                t.title.toLowerCase().includes(searchTerm.toLowerCase()) || 
                t.desc.toLowerCase().includes(searchTerm.toLowerCase()) ||
                t.tags.some(tag => tag.toLowerCase().includes(searchTerm.toLowerCase())) ||
                t.packages.some(pkg => pkg.toLowerCase().includes(searchTerm.toLowerCase()));
            return matchesCategory && matchesSearch;
        });

        if (filtered.length === 0) {
            const noResults = document.createElement('div');
            noResults.className = 'axonos-no-templates';
            noResults.style.gridColumn = '1 / -1';
            noResults.style.textAlign = 'center';
            noResults.style.padding = '2rem';
            noResults.style.color = 'var(--ink-mute)';
            noResults.style.fontFamily = 'var(--font-mono)';
            noResults.textContent = 'No matching templates found.';
            grid.appendChild(noResults);
            return;
        }

        const parser = new DOMParser();

        filtered.forEach(t => {
            const card = document.createElement('div');
            card.className = 'axonos-template-card';
            if (window.axonosSelectedTemplateId === t.id) {
                card.classList.add('selected');
            }

            // Header
            const header = document.createElement('div');
            header.className = 'axonos-template-header';

            const iconWrap = document.createElement('div');
            iconWrap.className = 'axonos-template-icon-wrap';
            try {
                const parsedSvg = parser.parseFromString(t.icon, 'image/svg+xml');
                iconWrap.appendChild(parsedSvg.documentElement);
            } catch (err) {
                iconWrap.textContent = '🧬';
            }

            const meta = document.createElement('div');
            meta.className = 'axonos-template-meta';
            t.tags.forEach(tag => {
                const tagEl = document.createElement('span');
                tagEl.className = 'axonos-template-tag';
                tagEl.textContent = tag;
                meta.appendChild(tagEl);
            });

            header.appendChild(iconWrap);
            header.appendChild(meta);
            card.appendChild(header);

            // Title
            const title = document.createElement('h4');
            title.className = 'axonos-template-title';
            title.textContent = t.title;
            card.appendChild(title);

            // Description
            const desc = document.createElement('p');
            desc.className = 'axonos-template-desc';
            desc.textContent = t.desc;
            card.appendChild(desc);

            // Image info
            const imageInfo = document.createElement('div');
            imageInfo.className = 'axonos-template-image-info';
            const code = document.createElement('code');
            code.textContent = t.image;
            imageInfo.appendChild(code);
            card.appendChild(imageInfo);

            // Actions
            const actions = document.createElement('div');
            actions.className = 'axonos-template-actions';

            const selectBtn = document.createElement('button');
            selectBtn.type = 'button';
            selectBtn.className = 'axonos-template-btn select-btn';
            if (window.axonosSelectedTemplateId === t.id) {
                selectBtn.textContent = 'Selected';
            } else {
                selectBtn.textContent = 'Select';
            }
            selectBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (window.axonosSelectedTemplateId === t.id) {
                    window.axonosSelectedTemplateId = null;
                } else {
                    window.axonosSelectedTemplateId = t.id;
                }
                UI.persistAxonosSelectedTemplate();
                UI.updateAxonosSelectedTemplateBanner();
                UI.renderAxonosTemplates(searchTerm, activeCategory, gridSelector);
            });

            const infoBtn = document.createElement('button');
            infoBtn.type = 'button';
            infoBtn.className = 'axonos-template-btn info-btn';
            infoBtn.textContent = 'Info';
            infoBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                UI.showAxonosTemplateDetails(t);
            });

            actions.appendChild(selectBtn);
            actions.appendChild(infoBtn);
            card.appendChild(actions);

            // Card click also selects/deselects
            card.addEventListener('click', () => {
                if (window.axonosSelectedTemplateId === t.id) {
                    window.axonosSelectedTemplateId = null;
                } else {
                    window.axonosSelectedTemplateId = t.id;
                }
                UI.persistAxonosSelectedTemplate();
                UI.updateAxonosSelectedTemplateBanner();
                UI.renderAxonosTemplates(searchTerm, activeCategory, gridSelector);
            });

            grid.appendChild(card);
        });
    },

    updateAxonosSelectedTemplateBanner() {
        const banner = document.getElementById('axonos_selected_template_banner');
        const iconEl = document.getElementById('axonos_selected_template_icon');
        const titleEl = document.getElementById('axonos_selected_template_title');
        if (!banner || !iconEl || !titleEl) return;

        const currentId = window.axonosSelectedTemplateId;
        if (!currentId) {
            iconEl.replaceChildren();
            iconEl.textContent = '🧬';
            titleEl.textContent = 'AxonOS Default Desktop';
            banner.style.borderColor = 'var(--rule-strong)';
            banner.style.boxShadow = 'none';
            return;
        }

        const template = AXONOS_TEMPLATES.find(t => t.id === currentId);
        if (template) {
            iconEl.replaceChildren();
            try {
                const parsedSvg = new DOMParser().parseFromString(template.icon, 'image/svg+xml');
                iconEl.appendChild(parsedSvg.documentElement);
            } catch (err) {
                iconEl.textContent = '🧬';
            }
            titleEl.textContent = template.title;
            banner.style.borderColor = 'var(--cyan)';
            banner.style.boxShadow = '0 0 12px rgba(78, 195, 212, 0.15)';
        } else {
            iconEl.replaceChildren();
            iconEl.textContent = '🧬';
            titleEl.textContent = 'AxonOS Default Desktop';
            banner.style.borderColor = 'var(--rule-strong)';
            banner.style.boxShadow = 'none';
        }
    },

    // ---- Direct SSH access toggle -----------------------------------------
    // When enabled, the session launches headless (no desktop/WebRTC) and the
    // landing page shows an `ssh ...` connect-string instead of the viewer.

    /** True when the user has opted into a direct-SSH session.
     *  Read the live checkbox first — it's the source of truth and survives the
     *  wallet-connect flow even when the window-global state gets reset. */
    axonosSshEnabled() {
        const t = document.getElementById('axonos_ssh_toggle');
        if (t) return !!t.checked;
        return !!window.axonosSshEnabled;
    },

    /** Trimmed public key the user pasted (empty string if none). Reads the live
     *  textarea first for the same reason as axonosSshEnabled(). */
    axonosSshPubkey() {
        const k = document.getElementById('axonos_ssh_pubkey');
        if (k && k.value && k.value.trim()) return k.value.trim();
        return (window.axonosSshPubkey || '').trim();
    },

    persistAxonosSshState() {
        // SSH mode is an explicit, per-launch opt-in. Never persist the toggle:
        // carrying a previous choice into a later launch can silently create a
        // headless session when the user expects a desktop. The public key alone
        // is safe to retain as a convenience for a future explicit opt-in.
        try {
            window.localStorage.removeItem('axonosSshEnabled');
            const key = (window.axonosSshPubkey || '').trim();
            if (key) {
                window.localStorage.setItem('axonosSshPubkey', key);
            } else {
                window.localStorage.removeItem('axonosSshPubkey');
            }
        } catch (e) { /* localStorage unavailable; selection just won't persist */ }
    },

    /** Reset SSH launch intent without discarding the user's saved public key. */
    resetAxonosSshLaunchIntent() {
        window.axonosSshEnabled = false;
        const toggle = document.getElementById('axonos_ssh_toggle');
        if (toggle) toggle.checked = false;
        UI.persistAxonosSshState();
        UI.updateAxonosSshUi();
    },

    /** Show/hide the key textarea and relabel the launch button to match the mode. */
    updateAxonosSshUi() {
        const keyWrap = document.getElementById('axonos_ssh_key_wrap');
        const toggle = document.getElementById('axonos_ssh_toggle');
        const on = !!window.axonosSshEnabled;
        if (toggle) toggle.checked = on;
        if (keyWrap) keyWrap.classList.toggle('axonos-ssh-key--hidden', !on);
        const connectText = document.querySelector('#noVNC_connect_button .axonos-connect-text');
        const connectSub = document.querySelector('#noVNC_connect_button .axonos-connect-subtext');
        if (connectText && connectSub) {
            if (on) {
                connectText.textContent = 'Launch Direct SSH Session';
                connectSub.textContent = 'Headless GPU shell — connect from your terminal';
            } else {
                connectText.textContent = 'Launch GPU-Native Desktop';
                connectSub.textContent = 'Full Linux environment with direct GPU access';
            }
        }
        // Keep the other desktop-worded copy in sync with the mode. These are only
        // shown transiently (the loader phase text, which is also SSH-aware, takes
        // over during an actual launch).
        const verifiedMsg = document.getElementById('axonos_wallet_verified_msg');
        if (verifiedMsg) {
            verifiedMsg.textContent = on ? 'Starting your SSH console…' : 'Connecting to desktop…';
        }
        const loaderSub = document.querySelector('#axonos-loader .axonos-loading-subtext');
        if (loaderSub) {
            loaderSub.textContent = on
                ? 'Setting up your headless GPU console…'
                : 'Connecting to AxonOS with direct GPU access…';
        }
    },

    initAxonosSshToggle() {
        // Always begin in desktop mode. A stale toggle saved by older builds is
        // removed so SSH must be selected explicitly for each new launch.
        try {
            window.localStorage.removeItem('axonosSshEnabled');
            window.axonosSshEnabled = false;
            window.axonosSshPubkey = window.localStorage.getItem('axonosSshPubkey') || '';
        } catch (e) {
            window.axonosSshEnabled = false;
            window.axonosSshPubkey = '';
        }

        const toggle = document.getElementById('axonos_ssh_toggle');
        const keyInput = document.getElementById('axonos_ssh_pubkey');
        if (keyInput && window.axonosSshPubkey) {
            keyInput.value = window.axonosSshPubkey;
        }
        if (toggle) {
            toggle.addEventListener('change', () => {
                window.axonosSshEnabled = toggle.checked;
                UI.persistAxonosSshState();
                UI.updateAxonosSshUi();
                if (toggle.checked && keyInput) keyInput.focus();
            });
        }
        if (keyInput) {
            keyInput.addEventListener('input', () => {
                window.axonosSshPubkey = keyInput.value;
                UI.persistAxonosSshState();
            });
        }

        const copyBtn = document.getElementById('axonos_ssh_copy_btn');
        if (copyBtn) {
            copyBtn.addEventListener('click', () => {
                const cmd = document.getElementById('axonos_ssh_connect_cmd');
                const text = cmd ? cmd.textContent : '';
                if (!text || text === '—') return;
                const done = () => { copyBtn.textContent = 'Copied'; setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500); };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(done).catch(() => {});
                }
            });
        }
        const endBtn = document.getElementById('axonos_ssh_end_btn');
        if (endBtn) {
            endBtn.addEventListener('click', () => {
                UI.hideAxonosSshCard();
                UI.disconnect();
            });
        }
        const webTerminalBtn = document.getElementById('axonos_ssh_web_terminal_btn');
        if (webTerminalBtn) {
            webTerminalBtn.addEventListener('click', () => {
                if (!UI._axonosSshClaim || webTerminalBtn.disabled) return;
                UI.openAxonosSshTerminal(UI._axonosSshClaim, { manual: true });
            });
        }
        const extendBtn = document.getElementById('axonos_ssh_extend_btn');
        if (extendBtn) {
            // Extend = owner re-claim: the gate renews the hard billing cap to
            // now + min(affordable, ceiling) and returns the fresh deadline.
            extendBtn.addEventListener('click', () => {
                if (extendBtn.disabled) return;
                const restoreLabel = extendBtn.textContent;
                extendBtn.disabled = true;
                extendBtn.textContent = 'Extending…';
                UI._axonosFetchSessionClaim().then((claim) => {
                    const granted = claim && (claim.granted === true || claim.granted === 'true');
                    if (granted && typeof claim.hard_cap_remaining_seconds === 'number') {
                        UI._axonosUpdateSshCardCap(claim);
                        extendBtn.textContent = 'Extended ✓';
                    } else if (granted) {
                        extendBtn.textContent = 'Extended ✓';
                    } else {
                        const reason = (claim && claim.reason) ? String(claim.reason) : 'Could not extend the session.';
                        UI.showStatus(reason, 'error');
                        extendBtn.textContent = restoreLabel;
                    }
                }).catch(() => {
                    UI.showStatus(_('Could not extend the session.'), 'error');
                    extendBtn.textContent = restoreLabel;
                }).finally(() => {
                    setTimeout(() => {
                        extendBtn.disabled = false;
                        extendBtn.textContent = restoreLabel;
                    }, 1600);
                });
            });
        }
        UI.updateAxonosSshUi();
    },

    /** Basic client-side sanity check so we fail fast before the claim round-trip. */
    axonosSshKeyLooksValid(key) {
        const k = (key || '').trim();
        if (!k || k.indexOf('\n') !== -1) return false;
        const parts = k.split(/\s+/);
        if (parts.length < 2 || parts.length > 3) return false;
        const types = ['ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384',
            'ecdsa-sha2-nistp521', 'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com'];
        return types.indexOf(parts[0]) !== -1 && /^[A-Za-z0-9+/=]+$/.test(parts[1]);
    },

    /** Hide/restore the launch controls so the active SSH session view is uncluttered. */
    _axonosToggleSshLaunchControls(hidden) {
        ['noVNC_connect_button', 'axonos_ssh_toggle_wrap', 'axonos_profile_picker_wrap'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.style.display = hidden ? 'none' : '';
        });
    },

    /** Render the SSH connect-string card from a granted SSH claim. */
    showAxonosSshCard(claim, options = {}) {
        const card = document.getElementById('axonos_ssh_connect_card');
        const cmdEl = document.getElementById('axonos_ssh_connect_cmd');
        if (!card || !cmdEl) return;
        const preserveScreen = options && options.preserveScreen === true;
        UI._axonosSshClaim = { ...(claim || {}), ssh_enabled: true };
        // Keep the landing dialog open (the card lives inside it) but hide the
        // launch controls while the session is active.
        UI.openConnectPanel();
        // The card lives in the LANDING screen section. A launch from the v2
        // wizard leaves the dialog in the wizard screen state (landing section
        // display:none), which rendered the connect-string invisibly — force
        // the landing screen whenever the card must be shown.
        const connectDialog = document.getElementById('noVNC_connect_dlg');
        if (connectDialog) connectDialog.classList.add('axonos-ssh-card-active');
        const landingAlreadyActive = !!(connectDialog &&
            connectDialog.classList.contains('axonos-state-landing'));
        if (!preserveScreen && !landingAlreadyActive &&
            typeof window.axonosUpdateActiveScreen === 'function') {
            window.axonosUpdateActiveScreen('landing');
        }
        const user = claim.ssh_user || 'aXonian';
        const host = claim.ssh_host || window.location.hostname;
        const port = claim.ssh_port;
        cmdEl.textContent = port
            ? `ssh -p ${port} ${user}@${host}`
            : 'External SSH endpoint unavailable';
        const copyBtn = document.getElementById('axonos_ssh_copy_btn');
        if (copyBtn) copyBtn.disabled = !port;
        const terminalBtn = document.getElementById('axonos_ssh_web_terminal_btn');
        if (terminalBtn) {
            terminalBtn.disabled = false;
            terminalBtn.textContent = 'Open web terminal';
        }
        UI._axonosSshFingerprintGeneration =
            (UI._axonosSshFingerprintGeneration || 0) + 1;
        if (UI._axonosSshFingerprintRetryTimer) {
            clearTimeout(UI._axonosSshFingerprintRetryTimer);
            UI._axonosSshFingerprintRetryTimer = null;
        }
        UI._axonosLoadSshHostFingerprint({
            generation: UI._axonosSshFingerprintGeneration,
            deadline: Date.now() + 60000
        });
        UI._axonosUpdateSshCardCap(claim);
        UI._axonosToggleSshLaunchControls(true);
        card.classList.remove('axonos-ssh-card--hidden');
    },

    async _axonosLoadSshHostFingerprint(options = {}) {
        const el = document.getElementById('axonos_ssh_host_fingerprint');
        if (!el) return;
        const generation = Number(options.generation) ||
            (UI._axonosSshFingerprintGeneration || 0);
        const deadline = Number(options.deadline) || (Date.now() + 60000);
        if (generation !== UI._axonosSshFingerprintGeneration) return;
        el.textContent = 'ED25519 host-key fingerprint: waiting for verification…';
        try {
            const headers = {};
            if (window.verifiedWalletAddress) {
                headers['X-Wallet-Address'] = window.verifiedWalletAddress;
            }
            if (window.verifiedWalletAuthToken) {
                headers['X-AXGT-Auth-Token'] = window.verifiedWalletAuthToken;
            }
            const response = await UI._axonosFetchJsonWithTimeout(
                new URL('/api/files/stats', window.location.origin).toString(),
                { credentials: 'include', headers },
                2500
            );
            if (generation !== UI._axonosSshFingerprintGeneration) return;
            const result = response && response.data;
            const fingerprint = result && result.ssh_host_key_fingerprint;
            if (typeof fingerprint !== 'string' || !fingerprint.startsWith('SHA256:')) {
                throw new Error('fingerprint unavailable');
            }
            el.textContent = `ED25519 host-key fingerprint: ${fingerprint}`;
        } catch (error) {
            if (generation !== UI._axonosSshFingerprintGeneration) return;
            if (Date.now() < deadline) {
                UI._axonosSshFingerprintRetryTimer = setTimeout(() => {
                    UI._axonosLoadSshHostFingerprint({ generation, deadline });
                }, 1500);
                return;
            }
            el.textContent = 'ED25519 host-key fingerprint unavailable — do not accept an unverified host key.';
        }
    },

    /** Update the SSH card deadline line from any payload that carries
     *  hard_cap_remaining_seconds (claim/status/heartbeat). The HARD cap is the
     *  real end time — it renews while an SSH connection is live and on Extend;
     *  the sliding idle TTL (remaining_seconds) is only a fallback for older
     *  gates that don't report the cap. Turns amber under 30 minutes. */
    _axonosUpdateSshCardCap(payload) {
        const ttlEl = document.getElementById('axonos_ssh_card_ttl');
        if (!ttlEl || !payload) return;
        const capSecs = (typeof payload.hard_cap_remaining_seconds === 'number')
            ? payload.hard_cap_remaining_seconds
            : null;
        const secs = capSecs !== null
            ? capSecs
            : (typeof payload.remaining_seconds === 'number' ? payload.remaining_seconds : null);
        if (secs === null) return;
        const mins = Math.max(0, Math.round(secs / 60));
        const h = Math.floor(mins / 60);
        const label = h > 0 ? `${h}h ${mins % 60}m` : `${mins} min`;
        ttlEl.textContent = capSecs !== null
            ? `Session ends in ~${label} — renews while you're connected over SSH, or press Extend.`
            : `Session time remaining: ~${label}`;
        ttlEl.style.color = mins <= 30 ? 'var(--warm, #f2c14e)' : '';
    },

    hideAxonosSshCard() {
        UI._axonosSshFingerprintGeneration =
            (UI._axonosSshFingerprintGeneration || 0) + 1;
        if (UI._axonosSshFingerprintRetryTimer) {
            clearTimeout(UI._axonosSshFingerprintRetryTimer);
            UI._axonosSshFingerprintRetryTimer = null;
        }
        const card = document.getElementById('axonos_ssh_connect_card');
        if (card) card.classList.add('axonos-ssh-card--hidden');
        const connectDialog = document.getElementById('noVNC_connect_dlg');
        if (connectDialog) connectDialog.classList.remove('axonos-ssh-card-active');
        UI._axonosToggleSshLaunchControls(false);
    },

    /** A dashboard already exposes the owned SSH command and retry action.
     *  Wizard/landing flows do not, so their recovery must reveal the dedicated
     *  SSH card instead of routing to a potentially empty workspace. */
    _axonosSshDashboardActive() {
        const connectDialog = document.getElementById('noVNC_connect_dlg');
        return !!(connectDialog &&
            connectDialog.classList.contains('axonos-state-dashboard'));
    },

    _axonosCloseTerminalClient() {
        UI._axonosTerminalOpenGeneration += 1;
        UI._axonosPendingTerminalClaim = null;
        if (UI._axonosTerminalOpenAbort) {
            try { UI._axonosTerminalOpenAbort.abort(); } catch (error) { /* ignore */ }
            UI._axonosTerminalOpenAbort = null;
        }
        const client = UI.terminalClient;
        UI.terminalClient = null;
        UI.terminalState = 'idle';
        if (UI.connectionKind === 'terminal') UI.connectionKind = null;
        document.documentElement.classList.remove('axonos-terminal-active');
        if (client && typeof client.close === 'function') {
            try {
                client.close();
            } catch (error) {
                Log.Warn('AxonOS terminal close failed: ' + error);
            }
        }
    },

    /** Keep an SSH allocation usable while wallet authentication is incomplete.
     *  Status probes can discover the address/session before verify-wallet has
     *  returned its auth token. Never spend a one-use terminal ticket in that
     *  gap; retain the claim and retry from axonosOnWalletVerified instead. */
    deferAxonosSshTerminalUntilAuthenticated(claim, options = {}) {
        const sshClaim = { ...(claim || {}), ssh_enabled: true };
        const preserveWorkspace = UI._axonosSshDashboardActive();
        UI._axonosSshClaim = sshClaim;
        UI._axonosPendingTerminalClaim = sshClaim;
        UI.terminalState = 'idle';
        if (UI.connectionKind === 'terminal') UI.connectionKind = null;
        document.documentElement.classList.remove('axonos-terminal-active');
        window.axonosSessionDetached = true;
        if (window.axonosOwnedSession) {
            window.axonosDetachedSession = window.axonosOwnedSession;
        }
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }
        UI.updateVisualState('disconnected');
        UI.openControlbar();
        UI.openConnectPanel();
        if (preserveWorkspace && typeof UI._axonosReturnToWorkspace === 'function') {
            UI._axonosReturnToWorkspace({
                refresh: options.refresh === true,
                reason: 'terminal-auth-pending',
            });
        }
        // A dashboard already contains a visible command/retry row. A fresh
        // wizard claim does not, so reveal the dedicated landing recovery card.
        UI.showAxonosSshCard(sshClaim, { preserveScreen: preserveWorkspace });
        UI.updateSessionControlButtons();
        if (options.notify !== false) {
            const walletKnown = !!String(window.verifiedWalletAddress || '').trim();
            UI.showStatus(
                walletKnown
                    ? (preserveWorkspace
                        ? _('Wallet sign-in is still finishing. The web terminal will open automatically; external SSH remains available in the workspace.')
                        : _('Wallet sign-in is still finishing. The web terminal will open automatically; external SSH is available on this page.'))
                    : _('Verify the session wallet to open the web terminal. External SSH remains available.'),
                'normal',
                8000
            );
        }
        return false;
    },

    resumePendingAxonosSshTerminal() {
        const claim = UI._axonosPendingTerminalClaim;
        const wallet = String(window.verifiedWalletAddress || '').trim();
        const authToken = String(window.verifiedWalletAuthToken || '').trim();
        if (!claim || !wallet || !authToken) return false;
        UI._axonosPendingTerminalClaim = null;
        return UI.openAxonosSshTerminal(claim, { authRetry: true });
    },

    _axonosTerminalErrorDetail(error) {
        const code = String((error && error.code) || '').trim().toLowerCase();
        const raw = String((error && error.message) || '').trim();
        if (/valid auth token|required.*auth|wallet verification/i.test(raw) ||
            code === 'wallet_required' || code === 'auth_pending') {
            return _('Wallet authentication is not ready. Reconnect the wallet and retry.');
        }
        if (code === 'ticket_timeout') {
            return _('The terminal authorization request timed out. Retry in a moment.');
        }
        if (code === 'connect_timeout') {
            return _('The secure terminal connection timed out. Retry in a moment.');
        }
        if (code === 'socket_failed' || code === 'socket_closed') {
            return _('The secure WebSocket could not be opened. Retry or use external SSH.');
        }
        if (code === 'unsafe_endpoint') {
            return _('The server returned an invalid terminal endpoint. Refresh before retrying.');
        }
        if (code === 'renderer_unavailable' ||
            /failed to fetch dynamically imported module|module script/i.test(raw)) {
            return _('The terminal viewer assets could not be loaded. Refresh before retrying.');
        }
        if (/failed to fetch|networkerror|network request failed/i.test(raw)) {
            return _('The terminal authorization request could not reach the server.');
        }
        // Server terminal errors are intentionally client-safe. Redact browser
        // capability/query values and wallet addresses before putting an
        // otherwise useful bounded reason in the visible status banner.
        return raw
            .replace(/([?&](?:ticket|auth_token)=)[^&\s]+/gi, '$1[redacted]')
            .replace(/\b0x[a-f0-9]{40}\b/gi, '[wallet]')
            .replace(/\s+/g, ' ')
            .trim()
            .slice(0, 180);
    },

    _axonosFallbackToSshCard(claim, error, options = {}) {
        const sshClaim = claim || UI._axonosSshClaim || {};
        const preserveWorkspace = UI._axonosSshDashboardActive();
        UI.connected = false;
        UI.terminalState = 'idle';
        UI._axonosPendingTerminalClaim = null;
        if (UI.connectionKind === 'terminal') UI.connectionKind = null;
        document.documentElement.classList.remove('axonos-terminal-active');
        window.axonosSessionDetached = true;
        if (window.axonosOwnedSession) {
            window.axonosDetachedSession = window.axonosOwnedSession;
        }
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }
        UI.updateVisualState('disconnected');
        UI.openControlbar();
        UI.openConnectPanel();
        if (preserveWorkspace && typeof UI._axonosReturnToWorkspace === 'function') {
            UI._axonosReturnToWorkspace({ refresh: false, reason: 'terminal-fallback' });
        }
        UI.showAxonosSshCard(sshClaim, {
            preserveScreen: preserveWorkspace,
        });
        if (!UI._axgtStatusPollId && window.verifiedWalletAddress &&
            window.verifiedWalletAuthToken) {
            UI._axgtStartSessionBillingPoll();
        }
        UI.updateSessionControlButtons();
        const externalAvailable = !!sshClaim.ssh_port;
        const detail = UI._axonosTerminalErrorDetail(error);
        if (options.normalExit === true) {
            UI.showStatus(
                _('Terminal closed — the SSH allocation remains available.'),
                'normal',
                5000
            );
        } else {
            UI.showStatus(
                externalAvailable
                    ? (detail
                        ? _(`Web terminal unavailable: ${detail} External SSH remains available ${preserveWorkspace ? 'in the workspace' : 'on this page'}.`)
                        : _(`Web terminal unavailable. External SSH remains available ${preserveWorkspace ? 'in the workspace' : 'on this page'}.`))
                    : (detail
                        ? _(`Web terminal unavailable: ${detail} No external SSH endpoint was returned; refresh the workspace and retry.`)
                        : _('Web terminal unavailable and no external SSH endpoint was returned. Refresh the workspace and retry.')),
                'warn',
                12000
            );
            if (detail) {
                const detailCode = error && error.code ? ` [${error.code}]` : '';
                Log.Warn('AxonOS terminal fallback' + detailCode + ': ' + detail);
            }
        }
    },

    /** Open a granted SSH-only allocation inside the shared viewer surface. */
    async openAxonosSshTerminal(claim, options = {}) {
        if (!claim || claim.ssh_enabled !== true) {
            UI.showStatus(_('The server did not identify this as an SSH session.'), 'error');
            return false;
        }
        const sshClaim = { ...claim };
        UI._axonosSshClaim = sshClaim;
        if (UI.connectionKind === 'terminal' && UI.terminalClient &&
            UI.terminalState === 'connected') {
            UI.terminalClient.focus();
            return true;
        }
        if (UI.terminalState === 'connecting') {
            return false;
        }
        const wallet = String(window.verifiedWalletAddress || '').trim();
        const authToken = String(window.verifiedWalletAuthToken || '').trim();
        if (!wallet || !authToken) {
            UI.deferAxonosSshTerminalUntilAuthenticated(sshClaim, {
                notify: options.authRetry !== true,
                refresh: false,
            });
            return false;
        }
        UI._axonosPendingTerminalClaim = null;

        const generation = ++UI._axonosTerminalOpenGeneration;
        const openAbort = typeof AbortController !== 'undefined'
            ? new AbortController()
            : null;
        UI._axonosTerminalOpenAbort = openAbort;
        UI.terminalState = 'connecting';
        const terminalBtn = document.getElementById('axonos_ssh_web_terminal_btn');
        if (terminalBtn) {
            terminalBtn.disabled = true;
            terminalBtn.textContent = 'Opening…';
        }
        if (options.manual === true) {
            UI.showStatus(_('Opening secure web terminal…'), 'normal', 2500);
        }

        try {
            const terminalModule = await import('./terminal/axonos-terminal.js?v=20260729d');
            const client = await terminalModule.openAxonosTerminal({
                container: document.getElementById('noVNC_container'),
                wallet,
                authToken,
                ...(openAbort ? { signal: openAbort.signal } : {}),
                onExit: (detail) => {
                    const code = detail && Number.isInteger(detail.code) ? detail.code : null;
                    UI.showStatus(
                        code === null ? _('Terminal process exited.') : _(`Terminal process exited (${code}).`),
                        code === 0 ? 'normal' : 'warn',
                        5000
                    );
                },
                onError: (terminalError) => {
                    Log.Warn('AxonOS terminal protocol error: ' + terminalError);
                },
                onClose: (detail) => {
                    if (generation !== UI._axonosTerminalOpenGeneration ||
                        detail.intentional === true) {
                        return;
                    }
                    UI.terminalClient = null;
                    const closeError = detail && detail.error instanceof Error
                        ? detail.error
                        : new Error(detail.reason || 'Terminal connection closed.');
                    UI._axonosFallbackToSshCard(
                        sshClaim,
                        closeError,
                        { normalExit: !!detail.exit }
                    );
                },
            });
            if (generation !== UI._axonosTerminalOpenGeneration ||
                String(window.verifiedWalletAddress || '').trim().toLowerCase() !==
                    wallet.toLowerCase()) {
                client.close();
                return false;
            }

            UI.terminalClient = client;
            UI.terminalState = 'connected';
            UI.connectionKind = 'terminal';
            document.documentElement.classList.add('axonos-terminal-active');
            // Deliberately do not set UI.connected: upstream noVNC treats that flag
            // as an RFB/WebRTC connection and routes keyboard/clipboard operations.
            UI.connected = false;
            window.axonosSessionDetached = false;
            window.axonosDetachedSession = null;
            UI.inhibitReconnect = true;
            UI.hideAxonosSshCard();
            UI.closeConnectPanel();
            UI.updateVisualState('connected');
            if (typeof window.axonosHideConnectionLoader === 'function') {
                window.axonosHideConnectionLoader(true);
            }
            if (!UI._axgtStatusPollId) UI._axgtStartSessionBillingPoll();
            UI.updateSessionControlButtons();
            UI.showStatus(_('Secure web terminal connected.'), 'normal', 2500);
            client.focus();
            return true;
        } catch (error) {
            if (generation !== UI._axonosTerminalOpenGeneration) return false;
            UI._axonosFallbackToSshCard(sshClaim, error);
            return false;
        } finally {
            if (UI._axonosTerminalOpenAbort === openAbort) {
                UI._axonosTerminalOpenAbort = null;
            }
            if (terminalBtn && generation === UI._axonosTerminalOpenGeneration &&
                UI.terminalState !== 'connecting') {
                terminalBtn.disabled = false;
                terminalBtn.textContent = 'Open web terminal';
            }
        }
    },

    showAxonosTemplateDetails(t) {
        const modal = document.getElementById('axonos_template_modal');
        const modalBody = document.getElementById('axonos_modal_body');
        if (!modal || !modalBody) return;

        modalBody.replaceChildren();
        const parser = new DOMParser();

        // Title wrap
        const titleWrap = document.createElement('div');
        titleWrap.className = 'axonos-m-title-wrap';

        const iconBox = document.createElement('div');
        iconBox.className = 'axonos-m-icon-box';
        try {
            const parsedSvg = parser.parseFromString(t.icon, 'image/svg+xml');
            iconBox.appendChild(parsedSvg.documentElement);
        } catch (err) {
            iconBox.textContent = '🧬';
        }

        const title = document.createElement('h3');
        title.className = 'axonos-m-title';
        title.id = 'axonos_modal_title';
        title.textContent = t.title;

        titleWrap.appendChild(iconBox);
        titleWrap.appendChild(title);
        modalBody.appendChild(titleWrap);

        // Tags
        const tagsWrap = document.createElement('div');
        tagsWrap.className = 'axonos-m-tags';
        t.tags.forEach(tag => {
            const tagEl = document.createElement('span');
            tagEl.className = 'axonos-template-tag';
            tagEl.textContent = tag;
            tagsWrap.appendChild(tagEl);
        });
        modalBody.appendChild(tagsWrap);

        // Description
        const desc = document.createElement('p');
        desc.className = 'axonos-m-desc';
        desc.textContent = t.desc;
        modalBody.appendChild(desc);

        // Docker Image Section
        const imgSec = document.createElement('div');
        imgSec.className = 'axonos-m-section';

        const imgSecTitle = document.createElement('h5');
        imgSecTitle.className = 'axonos-m-section-title';
        imgSecTitle.textContent = 'Unified Base Docker Image';
        imgSec.appendChild(imgSecTitle);

        const imgBox = document.createElement('div');
        imgBox.className = 'axonos-m-image-box';

        const imgCode = document.createElement('code');
        imgCode.textContent = 'axonos:public-beta';
        imgBox.appendChild(imgCode);

        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'axonos-m-copy-btn';
        copyBtn.textContent = 'Copy';
        copyBtn.addEventListener('click', () => {
            navigator.clipboard.writeText('axonos:public-beta').then(() => {
                copyBtn.textContent = 'Copied!';
                setTimeout(() => {
                    copyBtn.textContent = 'Copy';
                }, 2000);
            }).catch(err => {
                Log.Error('Could not copy docker image to clipboard: ' + err);
            });
        });
        imgBox.appendChild(copyBtn);
        imgSec.appendChild(imgBox);
        modalBody.appendChild(imgSec);

        // Env Variable Section
        const envSec = document.createElement('div');
        envSec.className = 'axonos-m-section';

        const envSecTitle = document.createElement('h5');
        envSecTitle.className = 'axonos-m-section-title';
        envSecTitle.textContent = 'Runtime Activation Variable';
        envSec.appendChild(envSecTitle);

        const envBox = document.createElement('div');
        envBox.className = 'axonos-m-image-box';

        const envCode = document.createElement('code');
        envCode.textContent = `AXONOS_SELECTED_TEMPLATE=${t.id}`;
        envBox.appendChild(envCode);

        const copyEnvBtn = document.createElement('button');
        copyEnvBtn.type = 'button';
        copyEnvBtn.className = 'axonos-m-copy-btn';
        copyEnvBtn.textContent = 'Copy';
        copyEnvBtn.addEventListener('click', () => {
            navigator.clipboard.writeText(`AXONOS_SELECTED_TEMPLATE=${t.id}`).then(() => {
                copyEnvBtn.textContent = 'Copied!';
                setTimeout(() => {
                    copyEnvBtn.textContent = 'Copy';
                }, 2000);
            }).catch(err => {
                Log.Error('Could not copy env variable to clipboard: ' + err);
            });
        });
        envBox.appendChild(copyEnvBtn);
        envSec.appendChild(envBox);
        modalBody.appendChild(envSec);

        // Pre-installed Packages
        const pkgSec = document.createElement('div');
        pkgSec.className = 'axonos-m-section';

        const pkgSecTitle = document.createElement('h5');
        pkgSecTitle.className = 'axonos-m-section-title';
        pkgSecTitle.textContent = 'Pre-installed Packages';
        pkgSec.appendChild(pkgSecTitle);

        const pkgGrid = document.createElement('div');
        pkgGrid.className = 'axonos-m-pkgs-grid';
        t.packages.forEach(pkg => {
            const pkgBadge = document.createElement('span');
            pkgBadge.className = 'axonos-m-pkg-badge';
            pkgBadge.textContent = pkg;
            pkgGrid.appendChild(pkgBadge);
        });
        pkgSec.appendChild(pkgGrid);
        modalBody.appendChild(pkgSec);

        // Shell Verification Command
        const verifySec = document.createElement('div');
        verifySec.className = 'axonos-m-section';

        const verifySecTitle = document.createElement('h5');
        verifySecTitle.className = 'axonos-m-section-title';
        verifySecTitle.textContent = 'Verification Command (inside container)';
        verifySec.appendChild(verifySecTitle);

        const cmdBox = document.createElement('div');
        cmdBox.className = 'axonos-m-command-box';

        const pre = document.createElement('pre');
        const codeEl = document.createElement('code');
        codeEl.textContent = t.verifyCmd;
        pre.appendChild(codeEl);
        cmdBox.appendChild(pre);

        verifySec.appendChild(cmdBox);
        modalBody.appendChild(verifySec);

        // Action section to select environment from details modal
        const actionSec = document.createElement('div');
        actionSec.className = 'axonos-m-section';
        actionSec.style.cssText = 'margin-top:24px;display:flex;justify-content:flex-end;';

        const selectBtn = document.createElement('button');
        selectBtn.type = 'button';
        selectBtn.className = 'axonos-cta axonos-cta--primary';
        selectBtn.style.cssText = 'padding:10px 22px;font-size:14px;cursor:pointer;border-radius:10px;font-weight:600;';
        selectBtn.textContent = 'Select Environment';
        selectBtn.addEventListener('click', () => {
            window.axonosSelectedTemplateId = t.id;
            UI.persistAxonosSelectedTemplate();
            UI.updateAxonosSelectedTemplateBanner();
            if (modal) {
                modal.classList.remove('active');
                modal.setAttribute('aria-hidden', 'true');
            }
            const wallet = window.verifiedWalletAddress;
            if (wallet) {
                if (typeof axonosStartWizard === 'function') {
                    axonosStartWizard();
                }
            } else {
                const dlg = document.getElementById('noVNC_credentials_dlg');
                if (dlg) dlg.classList.add('noVNC_open');
                if (typeof window.onConnectWalletClick === 'function') {
                    window.onConnectWalletClick();
                }
            }
        });
        actionSec.appendChild(selectBtn);
        modalBody.appendChild(actionSec);

        // Show Modal
        modal.classList.add('active');
        modal.setAttribute('aria-hidden', 'false');
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
                UI._axgtEndingSession = false;
                break;
            case 'connecting':
                UI._axgtEndingSession = false;
                transitionElem.textContent = _("Connecting...");
                document.documentElement.classList.add("noVNC_connecting");
                break;
            case 'connected':
                UI._axgtEndingSession = false;
                document.documentElement.classList.add("noVNC_connected");
                break;
            case 'disconnecting':
                if (UI._axgtEndingSession) {
                    transitionElem.textContent = _("Ending session...");
                } else {
                    transitionElem.textContent = _("Disconnecting...");
                }
                document.documentElement.classList.add("noVNC_disconnecting");
                break;
            case 'disconnected':
                UI._axgtEndingSession = false;
                break;
            case 'reconnecting':
                UI._axgtEndingSession = false;
                transitionElem.textContent = _("Reconnecting...");
                document.documentElement.classList.add("noVNC_reconnecting");
                break;
            default:
                Log.Error("Invalid visual state: " + state);
                UI.showStatus(_("Internal error"), 'error');
                return;
        }

        const viewerAttached = UI._axonosViewerAttached();
        if (viewerAttached) {
            if (UI.connectionKind !== 'terminal') UI.updateViewClip();

            UI.disableSetting('encrypt');
            UI.disableSetting('shared');
            UI.disableSetting('host');
            UI.disableSetting('port');
            UI.disableSetting('path');
            UI.disableSetting('repeaterID');

            // Hide the controlbar after 2 seconds
            UI.closeControlbarTimeout = setTimeout(UI.closeControlbar, 2000);
            UI.updateSessionControlButtons();
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
        if (UI._axgtUsageOverlayState !== 'locked') {
            document.getElementById('noVNC_credentials_dlg')
                .classList.remove('noVNC_open');
        }
    },

    showStatus(text, statusType, time) {
        const statusElem = document.getElementById('noVNC_status');

        if (typeof statusType === 'undefined') {
            statusType = 'normal';
        }

        // Always mirror to the console so the full sequence of states is
        // recoverable from devtools even when the banner has timed out or
        // been superseded on screen.
        if (statusType === 'error') {
            console.error('[AxonOS]', text);
        } else if (statusType === 'warn' || statusType === 'warning') {
            console.warn('[AxonOS]', text);
        } else {
            console.info('[AxonOS]', text);
        }

        // Don't let a routine message stomp a visible warning. Unlike upstream
        // noVNC we deliberately do NOT latch on the first error: a newer error
        // or warning may replace an older one, so recovery and follow-up states
        // can still reach the user instead of being swallowed by a stale banner.
        if (statusElem.classList.contains("noVNC_open") &&
            statusElem.classList.contains("noVNC_status_warn") &&
            statusType === 'normal') {
            return;
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

        // Errors get a longer dwell than routine toasts, but they do time out.
        // A permanently pinned red banner reads as a hung UI and has no
        // discoverable dismiss; the console mirror above keeps the detail.
        if (statusType === 'error') {
            time = Math.max(time, UI.STATUS_ERROR_TIMEOUT);
        }
        UI.statusTimeout = window.setTimeout(UI.hideStatus, time);
    },

    hideStatus() {
        clearTimeout(UI.statusTimeout);
        document.getElementById('noVNC_status').classList.remove("noVNC_open");
    },

    focusRemoteDesktop() {
        if (UI.connectionKind === 'terminal' && UI.terminalClient &&
            typeof UI.terminalClient.focus === 'function') {
            UI.terminalClient.focus();
            return;
        }
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
        UI.closeFilesPanel();
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

    showConfirm(title, message, options = {}) {
        return new Promise((resolve) => {
            const modal = document.getElementById('axonos_confirm_modal');
            const titleEl = document.getElementById('axonos_confirm_title');
            const msgEl = document.getElementById('axonos_confirm_body');
            const cancelBtn = document.getElementById('axonos_confirm_cancel');
            const okBtn = document.getElementById('axonos_confirm_ok');
            const overlay = document.getElementById('axonos_confirm_overlay');

            if (!modal || !titleEl || !msgEl || !cancelBtn || !okBtn) {
                resolve(window.confirm(title + "\n\n" + message));
                return;
            }

            titleEl.textContent = title;
            msgEl.textContent = message;

            okBtn.textContent = options.confirmText || _('OK');
            cancelBtn.textContent = options.cancelText || _('Cancel');

            okBtn.className = 'axonos-btn';
            if (options.confirmType === 'danger') {
                okBtn.classList.add('axonos-btn--danger');
            } else {
                okBtn.classList.add('axonos-btn--primary');
            }

            const cleanup = () => {
                modal.classList.remove('active');
                okBtn.removeEventListener('click', onOk);
                cancelBtn.removeEventListener('click', onCancel);
                overlay.removeEventListener('click', onCancel);
            };

            const onOk = () => {
                cleanup();
                resolve(true);
            };

            const onCancel = () => {
                cleanup();
                resolve(false);
            };

            okBtn.addEventListener('click', onOk);
            cancelBtn.addEventListener('click', onCancel);
            overlay.addEventListener('click', onCancel);

            modal.classList.add('active');
        });
    },

    async endSession() {
        if (!UI._axonosViewerAttached() && !window.axonosSessionDetached) {
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
            ? _("This stops billing for compute, ends your session, and tears down the desktop container. Your files in the home folder are safely saved (persistent storage is charged at " + storageCost + " minutes per GB/hour, accruing as debt when your balance is empty). To avoid volume deletion, clear your debt before it exceeds " + limitStr + ".")
            : _("This stops billing, ends your session, and removes your remote desktop. Unsaved work may be lost.");
        const confirmed = await UI.showConfirm(_("End session now?"), msg, {
            confirmText: _("End Session"),
            confirmType: 'danger'
        });
        if (!confirmed) {
            return;
        }
        window.axonosSessionDetached = false;
        UI.disconnect();
    },

    async detach() {
        if (!UI._axonosViewerAttached()) {
            return;
        }
        const terminalViewer = UI.connectionKind === 'terminal';
        const confirmed = await UI.showConfirm(
            terminalViewer ? _("Detach from the web terminal?") : _("Detach from the remote view?"),
            terminalViewer
                ? _("The current terminal and its foreground processes close, and you return to the SSH details card. The container keeps running and prepaid minutes keep counting. Use nohup or a session manager for work that must survive closing the terminal. Reopen the web terminal or use the external SSH command to return.\n\nUse End session when you are fully done.")
                : _("You return to the home screen, but your desktop and jobs keep running and prepaid minutes keep counting — even if you close this tab. Reconnect with the same wallet to return.\n\nUse End session when you are fully done. If credit runs out, viewer access and compute billing stop while the same running container, jobs, and GPUs are retained for the 2-hour top-up grace."),
            {
                confirmText: _("Detach"),
                confirmType: 'primary'
            }
        );
        if (!confirmed) {
            return;
        }
        UI.disconnect({ skipRelease: true, detach: true });
    },

    /** Swap the live session between desktop and SSH-console mode. The mode is
     *  baked into the container's runtime config (ports, desktop/WebRTC env), so
     *  a swap is a confirmed release of the current session followed by a fresh
     *  claim with the opposite requested_ssh — the wallet-keyed home volume and
     *  prepaid credits carry over on their own. The re-claim only runs after the
     *  server confirms the release; claiming earlier would be treated as an
     *  owner re-claim of the old session and silently keep the old mode. */
    async swapSessionMode() {
        if (!UI._axonosViewerAttached()) {
            return;
        }
        const toSsh = UI.connectionKind !== 'terminal';
        if (toSsh && !UI.axonosSshKeyLooksValid(UI.axonosSshPubkey())) {
            UI.showStatus(_('Add a valid SSH public key (e.g. the contents of ~/.ssh/id_ed25519.pub) in the launch options before relaunching as a console session.'), 'warn', 8000);
            return;
        }
        const confirmed = await UI.showConfirm(
            toSsh ? _("Relaunch as Console?") : _("Relaunch as Desktop?"),
            toSsh
                ? _("This ends the current desktop session (unsaved work in open applications is lost) and launches a headless SSH console session in its place. Files in your home folder and your remaining credits carry over.")
                : _("This ends the current console session (running shell processes stop) and launches a full desktop session in its place. Files in your home folder and your remaining credits carry over."),
            {
                confirmText: toSsh ? _("Relaunch as Console") : _("Relaunch as Desktop"),
                confirmType: 'danger'
            }
        );
        if (!confirmed) {
            return;
        }
        // Set the shared launch intent BEFORE releasing: both claim builders
        // (ui.js and the page's own) read the live toggle, and a stale value
        // would relaunch the old mode.
        const previousIntent = !!window.axonosSshEnabled;
        window.axonosSshEnabled = toSsh;
        UI.persistAxonosSshState();
        UI.updateAxonosSshUi();
        window.axonosSessionDetached = false;
        const released = await UI.disconnect({ releaseSource: 'swap-mode' });
        if (!released) {
            // Release unconfirmed: the old session may still be owned and
            // running (disconnect already surfaced recovery UI). Do not launch
            // a second session on top of it; restore the previous intent.
            window.axonosSshEnabled = previousIntent;
            UI.persistAxonosSshState();
            UI.updateAxonosSshUi();
            UI.showStatus(_('The current session could not be confirmed as released, so the relaunch was cancelled.'), 'error', 8000);
            return;
        }
        UI.showStatus(toSsh ? _('Launching console session…') : _('Launching desktop session…'), 'normal');
        // Let the post-release return-to-home transition settle before
        // relaunching through the single UI.connect choke point.
        setTimeout(() => UI.connect(), 1000);
    },

    async restartDesktopSession() {
        if (!UI.connected && !window.axonosSessionDetached) return;
        const wallet = window.verifiedWalletAddress;
        if (!wallet) {
            UI.showStatus(_("Wallet verification required"), 'error');
            return;
        }

        const confirmed = await UI.showConfirm(
            _("Restart desktop session now?"),
            _("Open apps in the remote desktop may close."),
            {
                confirmText: _("Restart"),
                confirmType: 'primary'
            }
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

    _axonosViewerAttached() {
        if (UI.connectionKind === 'terminal') {
            return UI.terminalState === 'connected' && !!UI.terminalClient;
        }
        return UI.connected && UI._axgtSessionDesktopActive();
    },

    updateSessionControlButtons() {
        const endBtn = document.getElementById('noVNC_power_button');
        const detachBtn = document.getElementById('noVNC_disconnect_button');
        if (!endBtn || !detachBtn) {
            return;
        }
        const viewOnly = UI._axonosViewerViewOnly();
        const viewerAttached = UI._axonosViewerAttached();
        const showEnd = (viewerAttached || window.axonosSessionDetached) && !viewOnly;
        const showDetach = viewerAttached && !window.axonosSessionDetached && !viewOnly;
        endBtn.classList.toggle('noVNC_hidden', !showEnd);
        detachBtn.classList.toggle('noVNC_hidden', !showDetach);
        UI.updateAxonosSwapButton();
    },

    /** Relabel the sidebar mode button for the attached viewer's mode: a web
     *  terminal (console session) offers "Relaunch as Desktop"; the desktop
     *  viewer offers "Relaunch as Console". Hidden while no live viewer is
     *  attached (the relaunch needs a current session to release and replace). */
    updateAxonosSwapButton() {
        const swapBtn = document.getElementById('axonos_sidebar_swap_btn');
        if (!swapBtn) {
            return;
        }
        const titleEl = document.getElementById('axonos_sidebar_swap_title');
        const descEl = document.getElementById('axonos_sidebar_swap_desc');
        const onConsole = UI.connectionKind === 'terminal';
        swapBtn.classList.toggle('noVNC_hidden', !UI._axonosViewerAttached());
        if (titleEl) {
            titleEl.textContent = onConsole ? _('Relaunch as Desktop') : _('Relaunch as Console');
        }
        if (descEl) {
            descEl.textContent = onConsole
                ? _('End this console and relaunch as a desktop session')
                : _('End this desktop and relaunch as an SSH console');
        }
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

/* ------^-------
 *   /CLIPBOARD
 * ==============
 *   FILES (panel)
 * ------v------*/

    _filesModule: null,

    addFilesHandlers() {
        const filesButton = document.getElementById("noVNC_files_button");
        if (filesButton) {
            filesButton.addEventListener('click', UI.toggleFilesPanel);
        }
    },

    async ensureFilesModule() {
        if (!UI._filesModule) {
            UI._filesModule = await import(`./files/axonos-files.js?v=${Date.now()}`);
        }
        return UI._filesModule;
    },

    openFilesPanel() {
        UI.closeAllPanels();
        UI.releaseWebRtcPointerState();
        UI.openControlbar();

        document.getElementById('noVNC_files')
            .classList.add("noVNC_open");
        document.getElementById('noVNC_files_button')
            .classList.add("noVNC_selected");

        UI.ensureFilesModule()
            .then((mod) => mod.onPanelOpen())
            .catch((err) => Log.Error('AxonOS files panel failed to load: ' + err));
    },

    closeFilesPanel() {
        const panel = document.getElementById('noVNC_files');
        if (!panel) return;
        panel.classList.remove("noVNC_open");
        document.getElementById('noVNC_files_button')
            .classList.remove("noVNC_selected");
    },

    toggleFilesPanel() {
        if (document.getElementById('noVNC_files')
            .classList.contains("noVNC_open")) {
            UI.closeFilesPanel();
        } else {
            UI.openFilesPanel();
        }
    },

/* ------^-------
 *   /FILES
 * ==============
 *   CLIPBOARD (receive)
 * ------v------*/

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

    _axonosInvalidateConnectAttempt() {
        UI._axonosConnectGeneration += 1;
        return UI._axonosConnectGeneration;
    },

    _axonosConnectAttemptIsCurrent(generation) {
        return generation === UI._axonosConnectGeneration;
    },

    /**
     * Return the connect overlay to the wallet-appropriate workspace. vnc.html owns
     * the full dashboard state machine; the local fallback keeps older theme copies
     * usable and, importantly, changes screen synchronously before any status fetch.
     */
    _axonosReturnToWorkspace(options) {
        const opts = options && typeof options === 'object' ? options : {};
        if (typeof window.axonosReturnToWorkspace === 'function') {
            try {
                const result = window.axonosReturnToWorkspace(opts);
                if (result && typeof result.catch === 'function') {
                    return result.catch((err) => {
                        Log.Warn('AxonOS workspace return hook failed: ' + err);
                    });
                }
                return result;
            } catch (err) {
                Log.Warn('AxonOS workspace return hook failed: ' + err);
            }
        }

        const wallet = window.verifiedWalletAddress;
        try {
            if (typeof window.axonosUpdateActiveScreen === 'function') {
                window.axonosUpdateActiveScreen(wallet ? 'dashboard' : 'landing');
            }
            if (wallet && opts.refresh !== false && typeof window.axonosLoadDashboard === 'function') {
                return window.axonosLoadDashboard();
            }
        } catch (err) {
            Log.Warn('AxonOS fallback workspace return failed: ' + err);
        }
        return undefined;
    },

    /** Await WebRTC cleanup, but never strand the UI on a hung close request. */
    _axonosAwaitWebRtcCleanup(cleanupPromise, timeoutMs = 2000) {
        if (!cleanupPromise || typeof cleanupPromise.then !== 'function') {
            return Promise.resolve();
        }
        let timeoutId = null;
        const timeout = new Promise((resolve) => {
            timeoutId = setTimeout(resolve, timeoutMs);
        });
        return Promise.race([
            Promise.resolve(cleanupPromise).catch((err) => {
                Log.Warn('AxonOS WebRTC cleanup failed: ' + err);
            }),
            timeout,
        ]).finally(() => {
            if (timeoutId !== null) {
                clearTimeout(timeoutId);
            }
        });
    },

    /** Fetch and consume a JSON response under one deadline; abort on timeout. */
    _axonosFetchJsonWithTimeout(url, options = {}, timeoutMs = 20000) {
        const controller = typeof AbortController !== 'undefined'
            ? new AbortController()
            : null;
        const requestOptions = { ...options };
        if (controller) {
            requestOptions.signal = controller.signal;
        }
        return new Promise((resolve, reject) => {
            let settled = false;
            let timer = null;
            const finish = (callback, value) => {
                if (settled) return;
                settled = true;
                if (timer !== null) clearTimeout(timer);
                callback(value);
            };
            timer = setTimeout(() => {
                if (controller) controller.abort();
                finish(reject, new Error('Request timed out'));
            }, Math.max(1000, Number(timeoutMs) || 20000));
            let request;
            try {
                request = fetch(url, requestOptions);
            } catch (error) {
                finish(reject, error);
                return;
            }
            request
                .then((response) => response.text().then((text) => {
                    let data = {};
                    if (text) {
                        try {
                            data = JSON.parse(text);
                        } catch (err) {
                            throw new Error(`HTTP ${response.status} returned non-JSON`);
                        }
                    }
                    return {
                        ok: response.ok,
                        status: response.status,
                        headers: response.headers,
                        data,
                    };
                }))
                .then(
                    (result) => finish(resolve, result),
                    (error) => finish(reject, error),
                );
        });
    },

    /**
     * Clear error banner, pending reconnect timer, and reconnect inhibition so the next
     * "Launch GPU-Native Desktop" can proceed. Mirrors the intent of the credit-exhaustion
     * path (reset gate) but without clearing wallet auth — use after dismissing a
     * capacity-unavailable response or
     * when recovering from a failed WS (e.g. 1006) before retrying.
     */
    axonosResetDesktopGateForRetry() {
        UI._axonosInvalidateConnectAttempt();
        UI._axonosCancelWebRtcClient();
        UI.hideStatus();
        UI.webrtcLastFailure = null;
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
    _axonosFetchSessionClaim(options) {
        const claimOptions = options && typeof options === 'object' ? options : {};
        const wallet = window.verifiedWalletAddress;
        if (!wallet) {
            return Promise.resolve({ granted: false, reason: 'No wallet' });
        }
        const payload = { wallet_address: wallet };
        const resumeMarker = window.axonosPausedResume;
        const resumeRequested = claimOptions.resumeOnly === true || !!resumeMarker;
        const expectedRaw = claimOptions.expectedSessionId != null
            ? claimOptions.expectedSessionId
            : (resumeMarker ? resumeMarker.sessionId : null);
        const expectedSessionId = Number(expectedRaw);
        if (resumeRequested) {
            if (!Number.isSafeInteger(expectedSessionId) || expectedSessionId <= 0) {
                return Promise.resolve({
                    granted: false,
                    resume_only: true,
                    invalid_resume_request: true,
                    reason: 'Retained session identity is unavailable. Refresh the workspace before reconnecting.',
                });
            }
            payload.resume_only = true;
            payload.expected_session_id = expectedSessionId;
        } else if (!window.axonosDetachedSession) {
            payload.requested_profile = (typeof window.axonosGetRequestedProfile === 'function')
                ? window.axonosGetRequestedProfile()
                : 'small';
            if (window.axonosSelectedTemplateId) {
                payload.requested_template = window.axonosSelectedTemplateId;
            }
            if (typeof window.axonosRequestedStorageGbForClaim === 'function') {
                const requestedStorageGb = window.axonosRequestedStorageGbForClaim(wallet);
                // null = capacity unknown: omit the field so the server keeps
                // the provisioned volume instead of rejecting a fabricated shrink.
                if (requestedStorageGb !== null) {
                    payload.requested_storage_gb = requestedStorageGb;
                }
            }
        }
        // SSH intent is sent on every claim (including reload re-claims) so the
        // gate can return the connect-string for an already-owned SSH session.
        if (UI.axonosSshEnabled()) {
            payload.requested_ssh = true;
            payload.ssh_pubkey = UI.axonosSshPubkey();
        }
        const url = new URL('/api/session/claim', window.location.origin).toString();
        const headers = {
            'Content-Type': 'application/json',
            'X-Wallet-Address': wallet,
        };
        if (window.verifiedWalletAuthToken) {
            headers['X-AXGT-Auth-Token'] = window.verifiedWalletAuthToken;
        }
        const timeoutMs = typeof window.axonosSessionClaimTimeoutMs === 'function'
            ? window.axonosSessionClaimTimeoutMs(resumeRequested)
            : (resumeRequested ? 20000 : 150000);
        return UI._axonosFetchJsonWithTimeout(url, {
            method: 'POST',
            credentials: 'include',
            headers,
            body: JSON.stringify(payload),
        }, timeoutMs).then((result) => result.data || {});
    },

    /** Reconcile an ambiguous claim without ever releasing its server-side session. */
    _axonosReconcileUncertainSessionClaim(options) {
        if (typeof window.axonosReconcileUncertainSessionClaim !== 'function') {
            return Promise.resolve({ recovered: false, authoritative: false });
        }
        try {
            return Promise.resolve(window.axonosReconcileUncertainSessionClaim(options))
                .catch((error) => ({ recovered: false, authoritative: false, error }));
        } catch (error) {
            return Promise.resolve({ recovered: false, authoritative: false, error });
        }
    },

    _axonosReleaseSessionHeaders(expectedWallet) {
        const wallet = window.verifiedWalletAddress;
        if (!wallet) {
            return null;
        }
        if (expectedWallet && String(wallet).trim().toLowerCase() !==
            String(expectedWallet).trim().toLowerCase()) {
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

    /**
     * Capture the non-secret identity of an explicit release attempt before any
     * disconnect caller is allowed to clear wallet/session UI state. The snapshot
     * is intentionally memory-only and never contains the wallet auth token.
     */
    _axonosSessionReleaseContext(options) {
        const opts = options && typeof options === 'object' ? options : {};
        const previous = opts.previous && typeof opts.previous === 'object'
            ? opts.previous : {};
        const source = opts.source || 'disconnect';
        const owned = window.axonosOwnedSession || window.axonosDetachedSession ||
            window.axonosPausedResume || window.axonosPendingResumeClaim || null;
        const sessionId = previous.sessionId || (owned && (
            owned.id || owned.session_id || owned.sessionId || owned.expectedSessionId
        )) || null;
        const viewerAttached = UI._axonosViewerAttached();
        UI._axonosSessionReleaseSequence = (UI._axonosSessionReleaseSequence || 0) + 1;
        const context = {
            attemptId: UI._axonosSessionReleaseSequence,
            retryOf: previous.attemptId || null,
            wallet: String(opts.wallet || previous.wallet || window.verifiedWalletAddress || '').trim(),
            sessionId: sessionId === null ? null : String(sessionId),
            requestedAt: Date.now(),
            source,
            // A retry is a new attempt, but it must retain whether the original
            // action was merely End session or a wallet sign-out/switch.
            intentSource: previous.intentSource || previous.source || source,
            connectionKind: previous.connectionKind || UI.connectionKind || null,
            wasDetached: previous.wasDetached === true || !!window.axonosSessionDetached,
            hadOwnedSession: previous.hadOwnedSession === true || !!owned,
            hadServerSession: previous.hadServerSession === true || !!owned ||
                viewerAttached || !!UI._axgtStatusPollId,
            released: null,
            failure: null,
            reason: null,
            status: null,
        };
        if (typeof window.axonosBeginSessionReleaseOperation === 'function') {
            context.operationId = window.axonosBeginSessionReleaseOperation(context);
        }
        return context;
    },

    _axonosNotifySessionReleaseResult(context) {
        if (typeof window.axonosSessionReleaseResultIsCurrent === 'function' &&
            !window.axonosSessionReleaseResultIsCurrent(context)) {
            return false;
        }
        const released = context && context.released === true;
        UI._axonosSessionReleaseFailureContext = released ? null : context;
        const hookName = released
            ? 'axonosHandleSessionReleaseSuccess'
            : 'axonosHandleSessionReleaseFailure';
        const hook = window[hookName];
        if (typeof hook === 'function') {
            try {
                const hookResult = hook(context);
                if (hookResult && typeof hookResult.catch === 'function') {
                    hookResult.catch((err) => {
                        Log.Warn('AxonOS session release result hook failed: ' + err);
                    });
                }
                return true;
            } catch (err) {
                Log.Warn('AxonOS session release result hook failed: ' + err);
            }
        }
        if (!released) {
            UI.showStatus(
                _('Could not confirm that the session ended. It may still be running and billing; retry End session.'),
                'error'
            );
        }
        return true;
    },

    _axonosSessionReleaseTimeoutMs() {
        const cfg = window.axonosConfig || {};
        const configuredMs = Number(cfg.session_release_timeout_ms);
        if (Number.isFinite(configuredMs) && configuredMs > 0) {
            return Math.max(15000, configuredMs);
        }
        const configuredSeconds = Number(cfg.session_release_timeout_seconds);
        if (Number.isFinite(configuredSeconds) && configuredSeconds > 0) {
            return Math.max(15000, configuredSeconds * 1000);
        }
        const launcherSeconds = Number(cfg.session_launcher_timeout_seconds);
        return Math.max(
            15000,
            (Number.isFinite(launcherSeconds) && launcherSeconds > 0
                ? launcherSeconds + 15 : 105) * 1000
        );
    },

    _axonosApplyConfirmedSessionRelease() {
        UI._axonosSessionReleaseFailureContext = null;
        window.axonosSessionDetached = false;
        window.axonosPendingResumeClaim = null;
        window.axonosPausedResume = null;
        if (typeof window.axonosClearDetachedSession === 'function') {
            window.axonosClearDetachedSession();
        }
        if (typeof window.axonosApplyResumeConnectUi === 'function') {
            window.axonosApplyResumeConnectUi(false);
        }
        if (UI._axgtStatusPollId) {
            clearInterval(UI._axgtStatusPollId);
            UI._axgtStatusPollId = null;
        }
        if (typeof UI._axgtStopSessionTimer === 'function') {
            UI._axgtStopSessionTimer();
        }
        UI.updateSessionControlButtons();
    },

    _axonosSessionOwnsServerSlot() {
        // Detached desktops and headless SSH sessions (which reuse the detached
        // flag) must SURVIVE tab close: that is the whole point of detaching, and
        // SSH sessions are used from a terminal with the in-container heartbeat
        // daemon keeping them alive. The container heartbeat keeps detached
        // compute active and billed across tab close; only End session / sign-out
        // releases it. Note the billing poll keeps running while detached, so
        // _axgtStatusPollId alone must not be treated as slot ownership here.
        if (window.axonosSessionDetached) {
            return false;
        }
        // Closing a browser terminal is equivalent to closing an external SSH
        // client, never an instruction to end the underlying compute allocation.
        if (UI.connectionKind === 'terminal' || UI.terminalState === 'connecting') {
            return false;
        }
        return !!(UI.connected || UI._axgtStatusPollId);
    },

    /** Fire-and-forget release for tab close (pagehide). */
    _axonosReleaseSessionBeacon() {
        const wallet = window.verifiedWalletAddress;
        const headers = UI._axonosReleaseSessionHeaders();
        if (!wallet || !headers) {
            return;
        }
        const url = new URL('/api/session/release', window.location.origin).toString();
        const payload = { wallet_address: wallet };
        const owned = window.axonosOwnedSession || window.axonosDetachedSession || null;
        const rawExpectedSessionId = owned && (
            owned.id || owned.session_id || owned.sessionId
        );
        if (rawExpectedSessionId !== null && rawExpectedSessionId !== undefined) {
            const expectedSessionId = Number(rawExpectedSessionId);
            if (!Number.isSafeInteger(expectedSessionId) || expectedSessionId <= 0) {
                Log.Warn('AxonOS pagehide release skipped: invalid session identity');
                return;
            }
            payload.expected_session_id = expectedSessionId;
        }
        const body = JSON.stringify(payload);
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

    /**
     * POST /api/session/release with a bounded wait. Resolves to true only when
     * the server confirms that the session was released or is already absent,
     * while enriching the supplied non-secret context with a recoverable failure
     * reason.
     */
    _axonosReleaseSessionBestEffort(context) {
        const releaseContext = context && typeof context === 'object'
            ? context : (UI._axonosPendingSessionReleaseContext ||
                UI._axonosSessionReleaseContext());
        const wallet = releaseContext.wallet;
        if (!wallet) {
            Object.assign(releaseContext, {
                released: false,
                failure: 'wallet_missing',
                reason: _('Wallet verification required to end the session.'),
                completedAt: Date.now(),
            });
            return Promise.resolve(false);
        }

        const url = new URL('/api/session/release', window.location.origin).toString();
        const headers = UI._axonosReleaseSessionHeaders(wallet);
        if (!headers) {
            Object.assign(releaseContext, {
                released: false,
                failure: 'wallet_changed',
                reason: _('The verified wallet changed before the session could be ended.'),
                completedAt: Date.now(),
            });
            return Promise.resolve(false);
        }

        return new Promise((resolve) => {
            let settled = false;
            let reconciliationTimer = null;
            let reconciliationAttempt = 0;
            const reconciliationDelays = [750, 1500, 3000];
            const controller = typeof AbortController === 'function'
                ? new AbortController() : null;
            const finish = (released, details) => {
                if (settled) return;
                settled = true;
                clearTimeout(timeoutId);
                if (reconciliationTimer) clearTimeout(reconciliationTimer);
                Object.assign(releaseContext, details || {}, {
                    released,
                    completedAt: Date.now(),
                });
                resolve(released);
            };
            const timeoutId = setTimeout(() => {
                if (controller) controller.abort();
                finish(false, {
                    failure: 'timeout',
                    reason: _('The session-end request timed out.'),
                });
            }, UI._axonosSessionReleaseTimeoutMs());

            // Container/network cleanup may keep the POST open after the session
            // row has authoritatively ended. Reconcile in parallel so the user is
            // not trapped behind the full-screen End state or shown a false timeout.
            const reconcileReleaseState = () => {
                if (settled || typeof window.axonosConfirmSessionReleaseState !== 'function') {
                    return;
                }
                Promise.resolve(window.axonosConfirmSessionReleaseState(releaseContext))
                    .then((outcome) => {
                        if (settled) return;
                        if (outcome && outcome.confirmed === true) {
                            finish(true, {
                                confirmedByStatus: true,
                                billingEnded: outcome.billingEnded === true,
                                failure: null,
                                reason: null,
                            });
                            return;
                        }
                        if (reconciliationAttempt < reconciliationDelays.length) {
                            reconciliationTimer = setTimeout(
                                reconcileReleaseState,
                                reconciliationDelays[reconciliationAttempt++]
                            );
                        }
                    }).catch(() => {
                        if (!settled && reconciliationAttempt < reconciliationDelays.length) {
                            reconciliationTimer = setTimeout(
                                reconcileReleaseState,
                                reconciliationDelays[reconciliationAttempt++]
                            );
                        }
                    });
            };
            reconciliationTimer = setTimeout(
                reconcileReleaseState,
                reconciliationDelays[reconciliationAttempt++]
            );

            const requestOptions = {
                method: 'POST',
                credentials: 'include',
                headers,
                body: null,
            };
            const requestBody = { wallet_address: wallet };
            if (releaseContext.sessionId !== null && releaseContext.sessionId !== undefined) {
                const expectedSessionId = Number(releaseContext.sessionId);
                if (!Number.isSafeInteger(expectedSessionId) || expectedSessionId <= 0) {
                    finish(false, {
                        failure: 'session_identity_invalid',
                        reason: _('The retained session identity is invalid; refresh the workspace before ending it.'),
                    });
                    return;
                }
                requestBody.expected_session_id = expectedSessionId;
            }
            requestOptions.body = JSON.stringify(requestBody);
            if (controller) requestOptions.signal = controller.signal;

            let request;
            try {
                request = fetch(url, requestOptions);
            } catch (err) {
                finish(false, {
                    failure: 'network',
                    reason: _('The session-end request could not be sent.'),
                });
                return;
            }
            Promise.resolve(request).then((response) => {
                const ct = response.headers.get('content-type') || '';
                const dataPromise = ct.includes('application/json')
                    ? response.json().catch(() => ({}))
                    : Promise.resolve({});
                return dataPromise.then((data) => ({ response, data }));
            }).then(({ response, data }) => {
                if (response.ok && data && data.released === true) {
                    finish(true, {
                        alreadyAbsent: data.already_absent === true,
                        failure: null,
                        reason: null,
                        status: response.status,
                    });
                    return;
                }
                const reason = data && (data.reason || data.error);
                // Release is intentionally not idempotent at the API layer: a
                // retry after an ambiguous timeout returns released:false once
                // the first request already ended the session. That authoritative
                // absence is still success for the user's requested end state.
                if (response.ok && reason &&
                    /^No active(?: or credit-grace)? session for this wallet$/i.test(String(reason).trim())) {
                    finish(true, {
                        alreadyAbsent: true,
                        failure: null,
                        reason: null,
                        status: response.status,
                    });
                    return;
                }
                finish(false, {
                    failure: 'rejected',
                    reason: reason ? String(reason) : _(
                        response.ok
                            ? 'The server did not confirm that the session ended.'
                            : 'The session-end request was rejected by the server.'
                    ),
                    status: response.status,
                    sessionMismatch: data && data.session_mismatch === true,
                    activeSessionId: data && data.active_session_id != null
                        ? String(data.active_session_id) : null,
                });
            }).catch((err) => {
                if (settled) return;
                finish(false, {
                    failure: 'network',
                    reason: _('The session-end request could not reach the server.'),
                });
            });
        });
    },

    /** Retry a failed explicit release without persisting or copying auth secrets. */
    retryAxonosSessionRelease(snapshot) {
        if (UI._axonosExplicitReleasePromise) {
            return UI._axonosExplicitReleasePromise;
        }
        const previous = snapshot && typeof snapshot === 'object'
            ? snapshot : UI._axonosSessionReleaseFailureContext;
        const context = UI._axonosSessionReleaseContext({
            previous,
            wallet: previous && previous.wallet,
            source: 'retry',
        });
        UI.showStatus(_('Ending session… Waiting for server confirmation.'), 'normal');
        const retryPromise = UI._axonosReleaseSessionBestEffort(context).then((released) => {
            if (typeof window.axonosSessionReleaseResultIsCurrent === 'function' &&
                !window.axonosSessionReleaseResultIsCurrent(context)) {
                return false;
            }
            if (released) UI._axonosApplyConfirmedSessionRelease();
            UI._axonosNotifySessionReleaseResult(context);
            return released;
        }).finally(() => {
            if (UI._axonosExplicitReleasePromise === retryPromise) {
                UI._axonosExplicitReleasePromise = null;
            }
        });
        UI._axonosExplicitReleasePromise = retryPromise;
        return retryPromise;
    },

    /** Cancel in-flight WebRTC negotiation and clear stale peer UI globals. */
    _axonosCancelWebRtcClient() {
        if (typeof window.axonosCancelWebRtcNegotiation === 'function') {
            try {
                return Promise.resolve(window.axonosCancelWebRtcNegotiation())
                    .catch((err) => {
                        Log.Warn('AxonOS WebRTC cancel failed: ' + err);
                    });
            } catch (err) {
                Log.Warn('AxonOS WebRTC cancel failed: ' + err);
            }
        }
        return Promise.resolve();
    },

    _axonosReturnToHomeAfterDisconnect(options) {
        const opts = options && typeof options === 'object' ? options : {};
        UI._axonosInvalidateConnectAttempt();
        UI._axonosCloseTerminalClient();
        UI.connectionKind = null;
        if (opts.resetWebRtc !== false) {
            UI._axonosCancelWebRtcClient();
        }
        UI.connected = false;
        if (!opts.preserveStatus && !opts.creditExhausted) {
            UI.hideStatus();
        }
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }
        UI.updateVisualState('disconnected');
        document.title = PAGE_TITLE;
        UI.openControlbar();
        UI.openConnectPanel();
        UI._axonosReturnToWorkspace({
            refresh: true,
            reason: opts.creditExhausted ? 'credit-exhausted' : 'disconnect',
        });
        // Clear any SSH connect card and restore the launch controls.
        if (typeof UI.hideAxonosSshCard === 'function') {
            UI.hideAxonosSshCard();
        }
        UI.updateSessionControlButtons();

        if (opts.creditExhausted) {
            UI.showStatus(
                _("Credit exhausted · 2h top-up grace. Jobs are still running; compute billing and viewer access have stopped. Add credit to reconnect."),
                'warn',
                12000
            );
        }

        // A wallet switch defers opening the wallet dialog + starting the new connect/verify
        // flow until the old desktop has fully returned home — otherwise the disconnect's
        // updateVisualState() (above) closes the wallet dialog out from under it. Run it now,
        // once, after the home view is settled.
        if (typeof window.axonosPendingWalletSwitch === 'function') {
            const runWalletSwitch = window.axonosPendingWalletSwitch;
            window.axonosPendingWalletSwitch = null;
            try {
                runWalletSwitch();
            } catch (err) {
                Log.Warn("AxonOS pending wallet switch failed: " + err);
            }
        }
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

    _axonosCompleteDetachUI(options) {
        const opts = options && typeof options === 'object' ? options : {};
        const releaseFailed = opts.releaseFailed === true ||
            !!UI._axonosSessionReleaseFailureContext;
        UI._axonosInvalidateConnectAttempt();
        UI._axonosCloseTerminalClient();
        UI.connectionKind = null;
        UI._axonosCancelWebRtcClient();
        UI.connected = false;
        if (!releaseFailed) UI.hideStatus();
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }
        UI.updateVisualState('disconnected');
        if (opts.creditExhausted) {
            UI.showStatus(
                _("Credit exhausted · 2h top-up grace. Jobs are still running; compute billing and viewer access have stopped. Add credit to reconnect."),
                'warn',
                12000
            );
        } else if (!releaseFailed) {
            UI.showStatus(
                _("Detached — desktop and jobs are still running, and billing continues even if this tab closes. Reconnect or End session when done."),
                'normal'
            );
        }
        document.title = PAGE_TITLE;
        UI.openControlbar();
        UI.openConnectPanel();
        UI._axonosReturnToWorkspace({
            refresh: true,
            reason: opts.creditExhausted ? 'credit-exhausted' : 'detach',
        });
        if (!UI._axgtStatusPollId && !opts.creditExhausted) {
            UI._axgtStartSessionBillingPoll();
        }
        UI.updateSessionControlButtons();
        if (typeof window.axonosSyncDetachedProfileUiImmediate === 'function') {
            window.axonosSyncDetachedProfileUiImmediate();
        }
        if (typeof window.axonosOnDetachedToHome === 'function') {
            window.axonosOnDetachedToHome();
        }
        if (opts.terminal === true && !opts.creditExhausted && UI._axonosSshClaim) {
            UI.showAxonosSshCard(UI._axonosSshClaim);
            UI.showStatus(_('Web terminal detached — the SSH allocation remains available.'), 'normal', 4000);
        }
    },

    _axonosCreateRfbConnection(password, includeQueryAuthToken, connectGeneration) {
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

        const rfb = new RFB(document.getElementById('noVNC_container'), url,
            { shared: UI.getSetting('shared'),
                repeaterID: UI.getSetting('repeaterID'),
                credentials: { password: password } });
        UI.rfb = rfb;
        rfb.addEventListener("connect", (e) => {
            if (UI.rfb === rfb && UI._axonosConnectAttemptIsCurrent(connectGeneration)) {
                UI.connectFinished(e);
            }
        });
        rfb.addEventListener("disconnect", (e) => {
            // A hard-reset RFB can emit after a replacement has already started.
            // Only the instance still owned by UI may finalize the disconnect UI.
            if (UI.rfb === rfb) {
                UI.disconnectFinished(e);
            }
        });
        rfb.addEventListener("credentialsrequired", UI.credentials);
        rfb.addEventListener("securityfailure", UI.securityFailed);
        rfb.addEventListener("capabilities", UI.updatePowerButton);
        rfb.addEventListener("clipboard", UI.clipboardReceive);
        rfb.addEventListener("bell", UI.bell);
        rfb.addEventListener("desktopname", UI.updateDesktopName);
        rfb.clipViewport = UI.getSetting('view_clip');
        rfb.scaleViewport = UI.getSetting('resize') === 'scale';
        rfb.resizeSession = UI.getSetting('resize') === 'remote';
        rfb.qualityLevel = parseInt(UI.getSetting('quality'));
        rfb.compressionLevel = parseInt(UI.getSetting('compression'));
        rfb.showDotCursor = UI.getSetting('show_dot');

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

        // Every asynchronous continuation below belongs to this exact Launch/Resume.
        // Disconnect, Cancel, or a newer connect invalidates it before it can touch UI.
        const connectGeneration = UI._axonosInvalidateConnectAttempt();
        const walletAtConnectStart = String(window.verifiedWalletAddress || '').trim();
        const connectAttemptIsCurrent = () =>
            UI._axonosConnectAttemptIsCurrent(connectGeneration);
        const pendingResumeClaim = window.axonosPendingResumeClaim;
        const pendingResumeMatchesWallet = !!(pendingResumeClaim &&
            String(pendingResumeClaim.wallet || '').toLowerCase() ===
                String(window.verifiedWalletAddress || '').toLowerCase());
        const pendingResumeExpectedId = pendingResumeMatchesWallet
            ? Number(pendingResumeClaim.expectedSessionId)
            : null;
        const pendingResumeClaimId = pendingResumeMatchesWallet
            ? Number(pendingResumeClaim.claim && pendingResumeClaim.claim.session_id)
            : null;
        const preclaimedResumeAtConnectStart = pendingResumeMatchesWallet &&
            Number.isSafeInteger(pendingResumeExpectedId) && pendingResumeExpectedId > 0 &&
            pendingResumeClaimId === pendingResumeExpectedId &&
            pendingResumeClaim.claim &&
            (pendingResumeClaim.claim.granted === true || pendingResumeClaim.claim.granted === 'true')
            ? pendingResumeClaim
            : null;
        // Resume intent is immutable for this connect attempt. A later status
        // refresh may fail or report expiry, but it must never downgrade this
        // operation into a spawn-capable ordinary claim.
        const resumeMarkerAtConnectStart = window.axonosPausedResume;
        const resumeIntentAtConnectStart = !!resumeMarkerAtConnectStart || pendingResumeMatchesWallet;
        const expectedResumeSessionIdAtConnectStart = pendingResumeMatchesWallet
            ? pendingResumeExpectedId
            : (resumeMarkerAtConnectStart ? Number(resumeMarkerAtConnectStart.sessionId) : null);

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
            setTimeout(() => {
                if (connectAttemptIsCurrent()) {
                    UI.connect(event, passwordArg);
                }
            }, 50);
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
        UI.webrtcLastFailure = null;

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
        // dismissed a capacity response and clicked connect — server returned 403 /
        // abnormal close (1006).
        const runSessionClaim = () => {
            if (!connectAttemptIsCurrent()) {
                return;
            }
            // Fail fast on a missing/invalid SSH key so the user gets a precise
            // message instead of a generic claim rejection round-trip.
            if (UI.axonosSshEnabled() && !UI.axonosSshKeyLooksValid(UI.axonosSshPubkey())) {
                if (typeof window.axonosHideConnectionLoader === 'function') {
                    window.axonosHideConnectionLoader(true);
                } else if (typeof window.axonosSetLaunchBusy === 'function') {
                    window.axonosSetLaunchBusy(false);
                }
                UI.updateVisualState('disconnected');
                UI.showStatus(_('Paste a valid SSH public key (e.g. the contents of ~/.ssh/id_ed25519.pub) to use SSH access.'), 'warn');
                const keyInput = document.getElementById('axonos_ssh_pubkey');
                if (keyInput) keyInput.focus();
                return;
            }
            if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                window.axonosSetConnectionLoaderPhase('claiming');
            }
            if (resumeIntentAtConnectStart && !preclaimedResumeAtConnectStart &&
                typeof window.axonosResumeDesktopConnectIfPaused === 'function' &&
                window.axonosResumeDesktopConnectIfPaused({
                    force: true,
                    expectedSessionId: expectedResumeSessionIdAtConnectStart,
                    walletPreflightDone: true
                })) {
                return;
            }
            if (preclaimedResumeAtConnectStart &&
                window.axonosPendingResumeClaim === pendingResumeClaim) {
                window.axonosPendingResumeClaim = null;
            }
            const claimRequest = preclaimedResumeAtConnectStart
                ? Promise.resolve(preclaimedResumeAtConnectStart.claim)
                : UI._axonosFetchSessionClaim(resumeIntentAtConnectStart ? {
                    resumeOnly: true,
                    expectedSessionId: expectedResumeSessionIdAtConnectStart,
                } : undefined);
            claimRequest.then((claim) => {
                const granted = claim && (claim.granted === true || claim.granted === 'true');
                if (!connectAttemptIsCurrent()) {
                    // Cancelling the browser flow cannot cancel a synchronous
                    // server launch. If it granted after this generation became
                    // stale, surface the resulting active/grace session instead
                    // of silently leaving hidden compute running and billed.
                    if (granted) {
                        UI._axonosReconcileUncertainSessionClaim({
                            wallet: walletAtConnectStart,
                            resumeOnly: resumeIntentAtConnectStart,
                            expectedSessionId: expectedResumeSessionIdAtConnectStart,
                            cancelled: true,
                        });
                    }
                    return;
                }
                if (!granted) {
                    if (typeof window.axonosHideConnectionLoader === 'function') {
                        window.axonosHideConnectionLoader(true);
                    } else if (typeof window.axonosSetLaunchBusy === 'function') {
                        window.axonosSetLaunchBusy(false);
                    }
                    UI.updateVisualState('disconnected');
                    // Surface the server's denial reason prominently and persistently —
                    // e.g. "Insufficient prepaid credit…" — so a credit/GPU denial never
                    // looks like a silent hang (it did, and cost a long debug).
                    const reason = (claim && claim.reason) ? String(claim.reason) : _('Could not claim desktop session.');
                    const hasReason = !!(claim && claim.reason);
                    UI.showStatus(reason, hasReason ? 'error' : 'warn');
                    if (typeof window.axonosOnSessionClaimDenied === 'function') {
                        // Pass the wallet this claim was made for so a denial
                        // that outlives a wallet switch cannot re-key the new
                        // wallet's storage-floor state.
                        window.axonosOnSessionClaimDenied(claim || {}, walletAtConnectStart);
                    }
                    return;
                }
                const computeSessionId = Number(claim && claim.session_id);
                if (!Number.isSafeInteger(computeSessionId) || computeSessionId <= 0) {
                    if (typeof window.axonosHideConnectionLoader === 'function') {
                        window.axonosHideConnectionLoader(true);
                    } else if (typeof window.axonosSetLaunchBusy === 'function') {
                        window.axonosSetLaunchBusy(false);
                    }
                    UI.updateVisualState('disconnected');
                    UI.showStatus(_('The session claim did not return a valid compute session ID.'), 'error');
                    return;
                }
                if (typeof window.axonosRememberOwnedSession === 'function') {
                    window.axonosRememberOwnedSession(claim);
                }
                if (claim && claim.ssh_enabled === true) {
                    // Surface the allocated external endpoint first. Opening the web
                    // terminal is an explicit choice on the ready card; hiding the
                    // command behind a terminal + Detach flow made direct SSH details
                    // effectively undiscoverable.
                    window.axonosSessionDetached = true;
                    if (window.axonosOwnedSession) {
                        window.axonosDetachedSession = window.axonosOwnedSession;
                    }
                    if (typeof window.axonosHideConnectionLoader === 'function') {
                        window.axonosHideConnectionLoader(true);
                    } else if (typeof window.axonosSetLaunchBusy === 'function') {
                        window.axonosSetLaunchBusy(false);
                    }
                    UI.updateVisualState('disconnected');
                    UI.openControlbar();
                    UI.showAxonosSshCard(claim);
                    if (!UI._axgtStatusPollId) UI._axgtStartSessionBillingPoll();
                    UI.updateSessionControlButtons();
                    UI.showStatus(_('SSH session ready — copy the command or open the web terminal.'), 'normal', 5000);
                    return;
                }
                if (claim && claim.resumed === true && typeof window.axonosRefreshPausedResumeStatus === 'function') {
                    window.axonosPausedResume = null;
                    window.axonosRefreshPausedResumeStatus();
                    UI.showStatus(_('Restored access to your running desktop session'), 'normal', 2500);
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
                    let webRtcFailure = null;
                    const cfgPeek = await UI._axonosFetchJsonWithTimeout(
                        './api/config',
                        { credentials: 'include' },
                        10000
                    )
                        .then((result) => result.ok ? (result.data || {}) : {})
                        .catch(() => ({}));
                    if (!connectAttemptIsCurrent()) {
                        return;
                    }
                    if (cfgPeek.webrtc_enabled) {
                        if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                            window.axonosSetConnectionLoaderPhase('webrtc');
                        }
                        let webRtcModule = null;
                        const onWebRtcProgress = (phase) => {
                            if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                                window.axonosSetConnectionLoaderPhase(phase);
                            }
                        };
                        const captureWebRtcFailure = (caughtError) => {
                            let failure = null;
                            if (webRtcModule &&
                                typeof webRtcModule.getAxonOSWebRTCFailure === 'function') {
                                try {
                                    failure = webRtcModule.getAxonOSWebRTCFailure();
                                } catch (failureErr) {
                                    Log.Warn('AxonOS WebRTC failure detail unavailable: ' + failureErr);
                                }
                            }
                            if (!failure && caughtError) {
                                const detail = String(
                                    (caughtError && caughtError.message) || caughtError
                                ).trim() || 'client error';
                                failure = {
                                    code: 'client_exception',
                                    detail,
                                    message: `WebRTC failed: ${detail}`,
                                    stage: 'client',
                                    state: 'failed',
                                    terminal: false,
                                    retryable: true,
                                };
                            }
                            webRtcFailure = failure && typeof failure === 'object'
                                ? { ...failure }
                                : null;
                            UI.webrtcLastFailure = webRtcFailure;
                            return webRtcFailure;
                        };
                        try {
                            // A stable module URL keeps negotiation generation/cancellation
                            // state shared across retries and rapid user reconnects.
                            webRtcModule = await import('./webrtc/axonos-webrtc.js?v=20260821c');
                            if (!connectAttemptIsCurrent()) {
                                return;
                            }
                            if (typeof webRtcModule.cancelAxonOSWebRTCNegotiation === 'function') {
                                window.axonosCancelWebRtcNegotiation = webRtcModule.cancelAxonOSWebRTCNegotiation;
                            }
                            usedWebRtc = await webRtcModule.connectAxonOSWebRTC({
                                UI,
                                computeSessionId,
                                onProgress: onWebRtcProgress,
                                // This attempt has an automatic retry. Keep its
                                // provisional error inside the loading experience.
                                deferFailureUi: true,
                            });
                            captureWebRtcFailure();
                        } catch (weErr) {
                            Log.Warn('AxonOS WebRTC path failed: ' + weErr);
                            captureWebRtcFailure(weErr);
                        }
                        if (!connectAttemptIsCurrent()) {
                            return;
                        }
                        const terminalWebRtcFailure = webRtcFailure && webRtcFailure.terminal === true;
                        if (!usedWebRtc && !window.axonosWebRtcConnectAborted &&
                            webRtcModule && !terminalWebRtcFailure) {
                            // One automatic retry on the same module instance. Clear the
                            // provisional ICE error so the retry starts from a clean banner
                            // rather than leaving a stale failure on screen during the wait.
                            Log.Warn('AxonOS: WebRTC negotiation failed; retrying once in 3s…');
                            UI.hideStatus();
                            if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                                window.axonosSetConnectionLoaderPhase('webrtc-retry');
                            }
                            await new Promise((res) => setTimeout(res, 3000));
                            if (connectAttemptIsCurrent() && !window.axonosWebRtcConnectAborted) {
                                try {
                                    usedWebRtc = await webRtcModule.connectAxonOSWebRTC({
                                        UI,
                                        computeSessionId,
                                        onProgress: onWebRtcProgress,
                                    });
                                    captureWebRtcFailure();
                                } catch (weErr2) {
                                    Log.Warn('AxonOS WebRTC retry failed: ' + weErr2);
                                    captureWebRtcFailure(weErr2);
                                }
                            }
                        } else if (!usedWebRtc && terminalWebRtcFailure) {
                            Log.Warn(
                                'AxonOS: terminal WebRTC failure; skipping automatic retry: ' +
                                (webRtcFailure.code || webRtcFailure.detail || 'unknown')
                            );
                        }
                        if (!connectAttemptIsCurrent()) {
                            return;
                        }
                        if (window.axonosWebRtcConnectAborted) {
                            if (typeof window.axonosHideConnectionLoader === 'function') {
                                window.axonosHideConnectionLoader(true);
                            }
                            return;
                        }
                        if (!usedWebRtc && cfgPeek.webrtc_fallback_enabled === false) {
                            const failureCode = webRtcFailure && String(webRtcFailure.code || '').trim();
                            const failureDetail = webRtcFailure && String(
                                webRtcFailure.detail || webRtcFailure.message || ''
                            ).trim();
                            let failureMessage;
                            if (failureCode === 'display_not_ready') {
                                failureMessage = _(
                                    'Desktop display failed to start (display_not_ready). ' +
                                    'The session is still running; end it from the workspace and launch again. ' +
                                    'If this repeats, contact support.'
                                );
                            } else {
                                const detailSuffix = failureDetail ? `: ${failureDetail}` : '';
                                failureMessage = _(
                                    `Could not connect to the desktop display${detailSuffix}. ` +
                                    'Retry from the workspace. If the session remains listed, end it before relaunching.'
                                );
                            }
                            // Terminal failure carrying an actionable server reason: give it
                            // a long explicit dwell instead of the default error timeout, and
                            // mark the workspace return status-preserving so its generic
                            // disconnect cleanup cannot hide the reason the user needs.
                            UI.hideStatus();
                            UI.showStatus(failureMessage, 'error', 30000);
                            UI._axonosReturnToHomeAfterDisconnect({ preserveStatus: true });
                            return;
                        }
                    }
                    if (!usedWebRtc && connectAttemptIsCurrent()) {
                        if (typeof window.axonosSetConnectionLoaderPhase === 'function') {
                            window.axonosSetConnectionLoaderPhase('vnc');
                        }
                        UI._axonosCreateRfbConnection(password, includeQueryAuthToken, connectGeneration);
                    }
                })();
            }).catch((err) => {
                Log.Error('AxonOS session claim failed: ' + err);
                const claimAttemptCancelled = !connectAttemptIsCurrent();
                UI._axonosReconcileUncertainSessionClaim({
                    wallet: walletAtConnectStart,
                    resumeOnly: resumeIntentAtConnectStart,
                    expectedSessionId: expectedResumeSessionIdAtConnectStart,
                    cancelled: claimAttemptCancelled,
                    error: err,
                }).then((recovery) => {
                    if ((recovery && recovery.recovered) || !connectAttemptIsCurrent()) {
                        return;
                    }
                    if (typeof window.axonosHideConnectionLoader === 'function') {
                        window.axonosHideConnectionLoader(true);
                    } else if (typeof window.axonosSetLaunchBusy === 'function') {
                        window.axonosSetLaunchBusy(false);
                    }
                    UI.updateVisualState('disconnected');
                    UI.showStatus(
                        resumeIntentAtConnectStart
                            ? _('Resume request could not be confirmed. The retained session was not released; retry safely.')
                            : _('Launch result could not be confirmed. Check the workspace for a running session before retrying.'),
                        'error'
                    );
                });
            });
        };

        // Conservative wallet preflight before claim/WebRTC. UI.connect is the single
        // choke point that fresh Launch (button click) and Resume (tryConnectAfterClaim ->
        // button click; axonosResumeDesktopConnectIfPaused) both pass through, so verifying
        // the exposed provider account here covers Launch / Resume / Claim. A mismatch or
        // unavailable provider blocks the action and delegates reconciliation to the page;
        // it is not, by itself, permission to release a retained server session.
        const proceedAfterPreflight = () => {
            if (!connectAttemptIsCurrent()) {
                return;
            }
            // The strict resume path already performed both wallet preflight and
            // the exact-session claim. Reuse that response immediately rather than
            // waiting on another status read or issuing a spawn-capable claim.
            if (preclaimedResumeAtConnectStart && pendingResumeClaim.walletPreflightDone === true) {
                runSessionClaim();
                return;
            }
            if (typeof window.axonosRefreshPausedResumeStatus === 'function') {
                window.axonosRefreshPausedResumeStatus()
                    .catch(() => null)
                    .finally(runSessionClaim);
            } else {
                runSessionClaim();
            }
        };

        // A transport recovery (gate redeploy / network blip) is not a new
        // grant of authority: the wallet was already verified for THIS session
        // and the session is still running. Re-verify silently so an outage
        // never interrupts the user with a wallet popup; a changed or locked
        // account still fails the check and routes to the normal flow.
        const recoveringExistingViewer = UI._axonosReconnectAttempt > 0
            && !UI.inhibitReconnect;
        if (preclaimedResumeAtConnectStart && pendingResumeClaim.walletPreflightDone === true) {
            proceedAfterPreflight();
        } else if (typeof window.axonosEnsureWalletSessionCurrent === 'function') {
            window.axonosEnsureWalletSessionCurrent({ requestPermission: !recoveringExistingViewer })
                .then((ok) => {
                    if (!connectAttemptIsCurrent()) {
                        return;
                    }
                    if (!ok) {
                        if (typeof window.axonosHideConnectionLoader === 'function') {
                            window.axonosHideConnectionLoader(true);
                        } else if (typeof window.axonosSetLaunchBusy === 'function') {
                            window.axonosSetLaunchBusy(false);
                        }
                        UI.updateVisualState('disconnected');
                        UI.showStatus(_('Wallet confirmation was cancelled or the active account changed. The running session was left unchanged.'), 'warn', 6000);
                        if (!window.verifiedWalletAddress) {
                            UI.credentials({ detail: { types: ['password'] } });
                        }
                        return;
                    }
                    proceedAfterPreflight();
                })
                .catch(() => {
                    if (!connectAttemptIsCurrent()) {
                        return;
                    }
                    // Preflight infrastructure error: fail safe (block).
                    if (typeof window.axonosHideConnectionLoader === 'function') {
                        window.axonosHideConnectionLoader(true);
                    } else if (typeof window.axonosSetLaunchBusy === 'function') {
                        window.axonosSetLaunchBusy(false);
                    }
                    UI.updateVisualState('disconnected');
                    UI.showStatus(_('Could not confirm the active wallet. The running session was left unchanged.'), 'warn', 6000);
                });
        } else {
            proceedAfterPreflight();
        }
    },

    disconnect(options) {
        const opts = options && typeof options === 'object' ? options : {};
        const skipRelease = opts.skipRelease === true;
        const detach = opts.detach === true;
        const creditExhausted = opts.creditExhausted === true;
        // Destructive controls can be reached from the viewer, SSH card, wallet
        // menu, and provider callbacks. Coalesce overlapping explicit releases so
        // a late failure cannot overwrite another attempt's confirmed success.
        if (!skipRelease && UI._axonosExplicitReleasePromise) {
            return UI._axonosExplicitReleasePromise;
        }
        const releaseContext = skipRelease ? null : UI._axonosSessionReleaseContext({
            source: typeof opts.releaseSource === 'string' ? opts.releaseSource : 'disconnect',
        });
        let releaseFailed = false;
        const terminalDisconnect = UI.connectionKind === 'terminal' ||
            UI.terminalState === 'connecting';

        if (!detach && !skipRelease) {
            UI._axgtEndingSession = true;
        } else {
            UI._axgtEndingSession = false;
        }

        // Set this before closing peer channels so their close handlers cannot race
        // an intentional End/Detach with an automatic reconnect.
        UI.inhibitReconnect = true;
        const disconnectGeneration = UI._axonosInvalidateConnectAttempt();
        // A terminal has no RFB disconnect event to finish this transition. Close
        // it synchronously before an End release, Detach, wallet cleanup, or credit
        // grace can leave an authenticated socket accepting input.
        if (terminalDisconnect) UI._axonosCloseTerminalClient();
        const webRtcCancelPromise = UI._axonosCancelWebRtcClient();
        if (typeof window.axonosHideConnectionLoader === 'function') {
            window.axonosHideConnectionLoader(true);
        }

        if (creditExhausted) {
            window.axonosSessionDetached = false;
            if (UI._axgtStatusPollId) {
                clearInterval(UI._axgtStatusPollId);
                UI._axgtStatusPollId = null;
            }
            if (typeof window.axonosClearDetachedSession === 'function') {
                window.axonosClearDetachedSession();
            }
        } else if (detach) {
            window.axonosSessionDetached = true;
            if (typeof window.axonosSyncDetachedProfileUiImmediate === 'function') {
                window.axonosSyncDetachedProfileUiImmediate();
            }
        } else if (!skipRelease) {
            window.axonosSessionDetached = false;
        }

        UI.connected = false;

        // Explicit release keeps the remembered server state until the response
        // is known. On ambiguity it becomes a detached, recoverable session;
        // detach/credit paths retain their existing immediate poll behavior.
        if (skipRelease && !detach && !window.axonosSessionDetached) {
            if (UI._axgtStatusPollId) {
                clearInterval(UI._axgtStatusPollId);
                UI._axgtStatusPollId = null;
            }
        }

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

        const disconnectIsCurrent = () =>
            UI._axonosConnectAttemptIsCurrent(disconnectGeneration);

        const finishWebRtcTeardown = () => {
            if (!disconnectIsCurrent()) {
                return;
            }
            if (typeof window.axonosWebRtcTeardown !== 'function') {
                doDisconnectRfb();
                return;
            }
            let teardownPromise;
            try {
                teardownPromise = window.axonosWebRtcTeardown();
            } catch (err) {
                Log.Warn('AxonOS WebRTC teardown failed: ' + err);
                doDisconnectRfb();
                return;
            }
            UI._axonosAwaitWebRtcCleanup(teardownPromise).finally(() => {
                if (disconnectIsCurrent()) {
                    doDisconnectRfb();
                }
            });
        };

        const doDisconnect = () => {
            UI._axonosAwaitWebRtcCleanup(webRtcCancelPromise)
                .finally(finishWebRtcTeardown);
        };

        const doDisconnectRfb = () => {
            if (!disconnectIsCurrent()) {
                return;
            }
            if (UI.rfb && typeof UI.rfb.disconnect === 'function') {
                try {
                    UI.rfb.disconnect();
                } catch (err) {
                    Log.Warn("AxonOS disconnect failed: " + err);
                    if (detach || window.axonosSessionDetached) {
                        UI._axonosCompleteDetachUI({
                            creditExhausted,
                            terminal: terminalDisconnect,
                            releaseFailed,
                        });
                    } else {
                        UI._axonosReturnToHomeAfterDisconnect({
                            creditExhausted,
                            preserveStatus: releaseFailed,
                        });
                    }
                }
            } else if (detach || window.axonosSessionDetached) {
                UI._axonosCompleteDetachUI({
                    creditExhausted,
                    terminal: terminalDisconnect,
                    releaseFailed,
                });
            } else {
                UI._axonosReturnToHomeAfterDisconnect({
                    creditExhausted,
                    preserveStatus: releaseFailed,
                });
            }
        };

        // Credit grace / Detach must not release the session (container retained).
        if (skipRelease) {
            doDisconnect();
            return Promise.resolve(true);
        }
        // Wait for a bounded, confirmed server-side release first so callers can
        // preserve identity and offer recovery when ownership remains ambiguous.
        UI._axonosPendingSessionReleaseContext = releaseContext;
        UI.showStatus(_('Ending session… Waiting for server confirmation.'), 'normal');
        const releasePromise = UI._axonosReleaseSessionBestEffort(releaseContext).then((released) => {
            if (typeof window.axonosSessionReleaseResultIsCurrent === 'function' &&
                !window.axonosSessionReleaseResultIsCurrent(releaseContext)) {
                return false;
            }
            releaseFailed = !released;
            if (released) {
                UI._axonosApplyConfirmedSessionRelease();
            } else if (releaseContext.hadServerSession) {
                // The viewer is closed below, but the unconfirmed server session
                // remains owned, visible, heartbeating, and retryable.
                window.axonosSessionDetached = true;
                UI._axgtEndingSession = false;
                if (typeof window.axonosSyncDetachedProfileUiImmediate === 'function') {
                    window.axonosSyncDetachedProfileUiImmediate();
                }
            }
            UI._axonosNotifySessionReleaseResult(releaseContext);
            return released;
        }).finally(() => {
            if (UI._axonosPendingSessionReleaseContext === releaseContext) {
                UI._axonosPendingSessionReleaseContext = null;
            }
            if (UI._axonosExplicitReleasePromise === releasePromise) {
                UI._axonosExplicitReleasePromise = null;
            }
            doDisconnect();
        });
        UI._axonosExplicitReleasePromise = releasePromise;
        return releasePromise;
    },

    /** Final billing heartbeat enters credit grace, then disconnect the viewer. */
    _axgtDisconnectForCreditExhaustion(overlayMessage) {
        UI.inhibitReconnect = true;
        if (UI.connectionKind === 'terminal' || UI.terminalState === 'connecting') {
            UI._axonosCloseTerminalClient();
        }
        if (typeof window !== 'undefined') {
            window.axonosAllowVncConnect = false;
        }
        if (UI._axgtStatusPollId) {
            clearInterval(UI._axgtStatusPollId);
            UI._axgtStatusPollId = null;
        }
        if (typeof window.axonosResetQueueClientState === 'function') {
            window.axonosResetQueueClientState();
        } else if (typeof window.axonosHideQueueOverlay === 'function') {
            window.axonosHideQueueOverlay();
        }
        const resumeHint = ' The same container, jobs, and GPUs keep running during the 2-hour top-up grace; compute billing and viewer access are stopped. Add credit before the grace expires, then reconnect.';
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
            setTimeout(() => UI.disconnect({ skipRelease: true, creditExhausted: true }), 400);
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
                const applyCreditGrace = window.axonosApplyCreditGraceResumeFromPayload ||
                    window.axonosApplyPausedResumeFromPayload;
                if (hb && (hb.credit_grace === true || hb.credit_grace_for_resume === true ||
                    hb.paused_for_resume === true) &&
                    typeof applyCreditGrace === 'function') {
                    applyCreditGrace(hb);
                }
                if (typeof window.axonosRefreshPausedResumeStatus === 'function') {
                    window.axonosRefreshPausedResumeStatus();
                }
            })
            .catch(() => {})
            .finally(finishDisconnect);
    },

    // Bounded exponential backoff for viewer recovery. A gate redeploy takes
    // ~40s before it accepts connections again, so a flat short retry would
    // hammer a listener that is still starting up. Reset on every successful
    // connect (see _axonosResetReconnectBackoff).
    _AXONOS_RECONNECT_BASE_MS: 2000,
    _AXONOS_RECONNECT_MAX_MS: 20000,
    _AXONOS_RECONNECT_MAX_ATTEMPTS: 12,
    _axonosReconnectAttempt: 0,

    _axonosResetReconnectBackoff() {
        UI._axonosReconnectAttempt = 0;
    },

    // Shared by the WebRTC recovery path so both transports back off together.
    _axonosRecoveryDelayMs() {
        UI._axonosReconnectAttempt += 1;
        return Math.min(
            UI._AXONOS_RECONNECT_MAX_MS,
            UI._AXONOS_RECONNECT_BASE_MS * Math.pow(2, UI._axonosReconnectAttempt - 1)
        );
    },

    _axonosScheduleRfbReconnect() {
        if (UI.inhibitReconnect) { return; }
        if (UI.reconnectCallback !== null && UI.reconnectCallback !== undefined) { return; }
        UI._axonosReconnectAttempt += 1;
        if (UI._axonosReconnectAttempt > UI._AXONOS_RECONNECT_MAX_ATTEMPTS) {
            UI._axonosResetReconnectBackoff();
            UI.updateVisualState('disconnected');
            UI.showStatus(_("Could not reconnect to your session"), 'error');
            UI._axonosReturnToHomeAfterDisconnect({ preserveStatus: true, resetWebRtc: false });
            return;
        }
        const delay = Math.min(
            UI._AXONOS_RECONNECT_MAX_MS,
            UI._AXONOS_RECONNECT_BASE_MS * Math.pow(2, UI._axonosReconnectAttempt - 1)
        );
        UI.reconnectCallback = setTimeout(UI.reconnect, delay);
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
        UI.inhibitReconnect = true;
        UI._axonosInvalidateConnectAttempt();
        UI._axonosCancelWebRtcClient();
        if (UI.reconnectCallback !== null) {
            clearTimeout(UI.reconnectCallback);
            UI.reconnectCallback = null;
        }

        UI.updateVisualState('disconnected');

        UI.openControlbar();
        UI.openConnectPanel();
        UI._axonosReturnToWorkspace({ refresh: true, reason: 'reconnect-cancelled' });
    },

    connectFinished(e) {
        UI.connected = true;
        UI.connectionKind = 'rfb';
        window.axonosSessionDetached = false;
        UI.inhibitReconnect = false;
        // A viewer that reached CONNECTED starts the next recovery from the
        // shortest delay rather than inheriting the previous outage's backoff.
        UI._axonosResetReconnectBackoff();
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
        UI._axonosInvalidateConnectAttempt();
        UI._axonosCancelWebRtcClient();
        UI.connected = false;
        UI.connectionKind = null;
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

        // An unclean drop of an established viewer is exactly the control-plane
        // restart case (gate redeploy): the session container and its heartbeat
        // daemon are still alive, so retry instead of dumping to the landing
        // screen. Intentional teardowns set inhibitReconnect and fall through.
        if (!e.detail.clean && wasConnected && !UI.inhibitReconnect) {
            UI.updateVisualState('reconnecting');
            UI.showStatus(_("Connection lost — reconnecting…"), 'warn');
            UI._axonosScheduleRfbReconnect();
            return;
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
        if (UI.connectionKind === 'terminal' && UI.terminalState === 'connected' &&
            window.verifiedWalletAddress) {
            return true;
        }
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
        const hud = document.getElementById('axonos_session_hud');
        if (hud) {
            hud.classList.add('axonos-session-hud--hidden');
            hud.setAttribute('aria-hidden', 'true');
        }
    },

    /** Top-right session HUD: wallet, billing rate and remaining credit time.
     *  Driven by the same 1 s tick / server anchor as the footer countdown;
     *  shown only while the remote desktop itself is on screen. */
    _axgtUpdateSessionHud(remainingSeconds, thresholdSeconds) {
        const hud = document.getElementById('axonos_session_hud');
        if (!hud) return;
        const show = UI._axgtSessionDesktopActive();
        hud.classList.toggle('axonos-session-hud--hidden', !show);
        hud.setAttribute('aria-hidden', show ? 'false' : 'true');
        if (!show) return;

        const walletEl = document.getElementById('axonos_session_hud_wallet');
        if (walletEl) {
            const addr = window.verifiedWalletAddress || '';
            walletEl.textContent = addr.length >= 12
                ? addr.slice(0, 6) + '…' + addr.slice(-4)
                : (addr || '—');
        }

        const rateEl = document.getElementById('axonos_session_hud_rate');
        if (rateEl) {
            const gpuBilling = window.axonosGpuBillingEnabled === true;
            const gpus = gpuBilling
                ? Math.max(1, Number(window.axonosBillingGpuCount || 1))
                : 1;
            const storage = window.axonosConfig &&
                window.axonosConfig.persistent_storage_enabled;
            rateEl.textContent = gpus + '× rate / min' +
                (storage ? ' + storage rate' : '');
        }

        const remEl = document.getElementById('axonos_session_hud_remaining');
        if (remEl) {
            remEl.textContent = (remainingSeconds / 60).toFixed(1) + ' min';
        }
        hud.classList.toggle('axonos-session-hud--warning',
            remainingSeconds > 60 && remainingSeconds <= thresholdSeconds);
        hud.classList.toggle('axonos-session-hud--critical',
            remainingSeconds <= 60);
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
        UI._axgtUpdateSessionHud(remaining, threshold);
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
        if (UI._axonosViewerAttached() || typeof window.axonosWebRtcTeardown === 'function') {
            UI.disconnect({ skipRelease: true });
            return;
        }
        UI.inhibitReconnect = true;
        UI._axonosInvalidateConnectAttempt();
        UI._axonosCancelWebRtcClient();
        UI.updateVisualState('disconnected');
        UI.openControlbar();
        UI.openConnectPanel();
        UI._axonosReturnToWorkspace({ refresh: true, reason: 'usage-overlay-exit' });
    },

    _axgtPollWalletStatus() {
        if (!window.verifiedWalletAddress || !UI._axgtSessionBillingActive()) {
            return;
        }
        const wallet = window.verifiedWalletAddress;
        const token = window.verifiedWalletAuthToken || null;
        const walletNormalized = String(wallet).trim().toLowerCase();
        const pollIdentityIsCurrent = () =>
            String(window.verifiedWalletAddress || '').trim().toLowerCase() === walletNormalized;
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
                if (!pollIdentityIsCurrent()) return;
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
                // Live SSH-card deadline: heartbeats carry the (possibly
                // presence-renewed) hard-cap remaining time.
                if (hb && hb.ok === true && window.axonosSessionDetached &&
                    typeof hb.hard_cap_remaining_seconds === 'number') {
                    UI._axonosUpdateSshCardCap(hb);
                }
                if (hb && hb.ok === false) {
                    const hbReason = String(hb.reason || '');
                    if (/credit exhausted/i.test(hbReason)) {
                        const applyCreditGrace = window.axonosApplyCreditGraceResumeFromPayload ||
                            window.axonosApplyPausedResumeFromPayload;
                        if ((hb.credit_grace === true || hb.credit_grace_for_resume === true ||
                            hb.paused_for_resume === true) &&
                            typeof applyCreditGrace === 'function') {
                            applyCreditGrace(hb);
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
                if (!pollIdentityIsCurrent()) return;
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

// Expose UI on the global scope so the (non-module) inline scripts in vnc.html
// can drive session control — disconnect, teardown, connected state, etc.
// ui.js is loaded as an ES module, so `UI` is otherwise module-scoped and the
// inline wallet/session code's bare `UI.*` references silently fail to resolve.
if (typeof window !== 'undefined') {
    window.UI = UI;
    window.axonosRetrySessionRelease = (snapshot) =>
        UI.retryAxonosSessionRelease(snapshot);
}

export default UI;

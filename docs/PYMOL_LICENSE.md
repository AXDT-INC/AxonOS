# PyMOL Open-Source License and Trademark Notice

AxonOS includes PyMOL (open-source) for molecular visualization. This component is **not** the commercial PyMOL product distributed by Schrödinger, LLC.

## Open-Source PyMOL Copyright and License

Open-Source PyMOL is Copyright (C) Schrodinger, LLC. All Rights Reserved.

Permission to use, copy, modify, distribute, and distribute modified versions of this software and its built-in documentation for any purpose and without fee is hereby granted, provided that the above copyright notice appears in all copies and that both the copyright notice and this permission notice appear in supporting documentation, and that the name of Schrodinger, LLC not be used in advertising or publicity pertaining to distribution of the software without specific, written prior permission.

SCHRODINGER, LLC DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL SCHRODINGER, LLC BE LIABLE FOR ANY SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## PyMOL Trademark Notice

**PyMOL(TM)** is a trademark of Schrodinger, LLC. This product includes PyMOL(TM) source code and is plainly distinguished from PyMOL products distributed by Schrodinger, LLC.

*PyMOL is a trademark of Schrodinger, LLC.*

---

## GPU-accelerated rendering (NVIDIA)

When the container is run with **`--gpus all`** (and the host has NVIDIA drivers), PyMOL is launched with **VirtualGL** (`vglrun`) so that OpenGL rendering uses the GPU:

- A headless X server (display **:0**) is started with the NVIDIA driver for 3D.
- The desktop runs on display **:0**. In launcher-managed sessions it is streamed to the browser by the WebRTC agent; only the legacy single-container mode also serves it via x11vnc on port 5901. PyMOL runs OpenGL directly on **:0** (GPU) in both cases.
- If no GPU is present, PyMOL falls back to software rendering and the container still runs.

Ensure the host has the NVIDIA driver and the container is started with `docker run --gpus all ...` (or equivalent) for GPU-accelerated molecular viewing.

---

Source: [schrodinger/pymol-open-source](https://github.com/schrodinger/pymol-open-source). See the repository for the full license and notices.

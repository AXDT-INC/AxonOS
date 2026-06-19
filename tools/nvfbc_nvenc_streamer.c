/*
 * NvFBC -> NVENC H.264 stdout streamer.
 *
 * Build with:
 *   gcc -O2 -Wall -I/path/to/Capture_Linux_v7.1.9/NvFBC/inc \
 *       tools/nvfbc_nvenc_streamer.c -lGL -lX11 -ldl -o /tmp/nvfbc_nvenc_streamer
 *
 * This file intentionally depends on user-provided NVIDIA Capture SDK headers
 * and does not vendor those headers into the repository.
 *
 * Portions follow NVIDIA Capture SDK sample patterns:
 *
 * Copyright (c) 2017-2018, NVIDIA CORPORATION. All rights reserved.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

#include <NvFBC.h>
#include <nvEncodeAPI.h>

#include <GL/gl.h>
#include <GL/glx.h>
#include <X11/Xlib.h>

#include <dlfcn.h>
#include <errno.h>
#include <getopt.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

#define LIB_NVFBC_NAME "libnvidia-fbc.so.1"
#define LIB_ENCODEAPI_NAME "libnvidia-encode.so.1"

typedef NVENCSTATUS(NVENCAPI *PFNNVENCODEAPICREATEINSTANCEPROC)(
    NV_ENCODE_API_FUNCTION_LIST *);

typedef enum {
    PROFILE_BASELINE,
    PROFILE_MAIN,
    PROFILE_HIGH,
    PROFILE_CONSTRAINED_HIGH,
} h264_profile;

typedef struct {
    unsigned int frames;
    NVFBC_SIZE size;
    unsigned int fps;
    unsigned int bitrate;
    unsigned int max_bitrate;
    unsigned int vbv_frames;
    unsigned int gop;
    unsigned int timeout_ms;
    unsigned int sampling_ms;
    int cursor;
    int push_model;
    int quiet;
    int mpegts;
    const char *preset_name;
    h264_profile profile;
} stream_options;

typedef struct {
    unsigned int pat_cc;
    unsigned int pmt_cc;
    unsigned int video_cc;
} ts_mux;

typedef struct {
    Display *display;
    Pixmap pixmap;
    GLXPixmap glx_pixmap;
    GLXContext context;
    GLXFBConfig fb_config;
} gl_state;

static gl_state g_gl;
static NV_ENCODE_API_FUNCTION_LIST g_enc;

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void sleep_until_ns(uint64_t target_ns) {
    uint64_t current = now_ns();
    if (target_ns <= current) {
        return;
    }
    uint64_t sleep_ns = target_ns - current;
    struct timespec req;
    req.tv_sec = (time_t)(sleep_ns / 1000000000ull);
    req.tv_nsec = (long)(sleep_ns % 1000000000ull);
    while (nanosleep(&req, &req) != 0 && errno == EINTR) {
    }
}

static int write_all(int fd, const void *data, size_t size) {
    const uint8_t *p = (const uint8_t *)data;
    while (size > 0) {
        ssize_t n = write(fd, p, size);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (errno == EPIPE) {
                return 0;
            }
            perror("write");
            return -1;
        }
        if (n == 0) {
            return -1;
        }
        p += (size_t)n;
        size -= (size_t)n;
    }
    return 1;
}

static void usage(const char *name) {
    fprintf(stderr,
            "usage: %s [options]\n"
            "\n"
            "Options:\n"
            "  --frames N             Number of frames, 0 means forever (default: 0)\n"
            "  --size WxH             Capture size, default is framebuffer size\n"
            "  --fps N                Output cadence (default: 30)\n"
            "  --bitrate BPS          Average bitrate (default: 12000000)\n"
            "  --max-bitrate BPS      Max bitrate (default: bitrate)\n"
            "  --vbv-frames N         VBV size in frames (default: 1)\n"
            "  --gop N                IDR interval in frames (default: fps)\n"
            "  --timeout-ms N         NvFBC grab timeout (default: one frame)\n"
            "  --preset NAME          llhp, llhq, ll, hp, hq, default (default: llhp)\n"
            "  --profile NAME         baseline, main, high, constrained-high (default: baseline)\n"
            "  --mux NAME             h264 or mpegts (default: h264)\n"
            "  --no-cursor            Do not composite cursor into video\n"
            "  --no-push              Disable NvFBC push model\n"
            "  --quiet                Suppress startup/progress logs\n",
            name);
}

static int parse_size(const char *raw, NVFBC_SIZE *size) {
    unsigned int w = 0, h = 0;
    if (sscanf(raw, "%ux%u", &w, &h) != 2 || w == 0 || h == 0) {
        return 0;
    }
    size->w = w;
    size->h = h;
    return 1;
}

static int parse_u32(const char *raw, unsigned int min, unsigned int max,
                     unsigned int *out) {
    char *end = NULL;
    unsigned long value = strtoul(raw, &end, 10);
    if (!raw[0] || (end && *end) || value < min || value > max) {
        return 0;
    }
    *out = (unsigned int)value;
    return 1;
}

static GUID preset_guid(const char *name) {
    if (!strcasecmp(name, "llhq")) {
        return NV_ENC_PRESET_LOW_LATENCY_HQ_GUID;
    }
    if (!strcasecmp(name, "ll") || !strcasecmp(name, "default-ll")) {
        return NV_ENC_PRESET_LOW_LATENCY_DEFAULT_GUID;
    }
    if (!strcasecmp(name, "hp")) {
        return NV_ENC_PRESET_HP_GUID;
    }
    if (!strcasecmp(name, "hq")) {
        return NV_ENC_PRESET_HQ_GUID;
    }
    if (!strcasecmp(name, "default")) {
        return NV_ENC_PRESET_DEFAULT_GUID;
    }
    return NV_ENC_PRESET_LOW_LATENCY_HP_GUID;
}

static GUID profile_guid(h264_profile profile) {
    switch (profile) {
    case PROFILE_MAIN:
        return NV_ENC_H264_PROFILE_MAIN_GUID;
    case PROFILE_HIGH:
        return NV_ENC_H264_PROFILE_HIGH_GUID;
    case PROFILE_CONSTRAINED_HIGH:
        return NV_ENC_H264_PROFILE_CONSTRAINED_HIGH_GUID;
    case PROFILE_BASELINE:
    default:
        return NV_ENC_H264_PROFILE_BASELINE_GUID;
    }
}

static int parse_profile(const char *raw, h264_profile *profile) {
    if (!strcasecmp(raw, "baseline")) {
        *profile = PROFILE_BASELINE;
        return 1;
    }
    if (!strcasecmp(raw, "main")) {
        *profile = PROFILE_MAIN;
        return 1;
    }
    if (!strcasecmp(raw, "high")) {
        *profile = PROFILE_HIGH;
        return 1;
    }
    if (!strcasecmp(raw, "constrained-high") || !strcasecmp(raw, "chigh")) {
        *profile = PROFILE_CONSTRAINED_HIGH;
        return 1;
    }
    return 0;
}

static uint32_t mpeg_crc32(const uint8_t *data, size_t len) {
    uint32_t crc = 0xffffffffu;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint32_t)data[i] << 24;
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 0x80000000u) {
                crc = (crc << 1) ^ 0x04c11db7u;
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

static void put_pts(uint8_t *p, uint64_t pts90) {
    uint64_t pts = pts90 & 0x1ffffffffull;
    p[0] = (uint8_t)(0x20 | (((pts >> 30) & 0x07) << 1) | 1);
    p[1] = (uint8_t)(pts >> 22);
    p[2] = (uint8_t)((((pts >> 15) & 0x7f) << 1) | 1);
    p[3] = (uint8_t)(pts >> 7);
    p[4] = (uint8_t)(((pts & 0x7f) << 1) | 1);
}

static void put_pcr(uint8_t *p, uint64_t pcr90) {
    uint64_t base = pcr90 & 0x1ffffffffull;
    p[0] = (uint8_t)(base >> 25);
    p[1] = (uint8_t)(base >> 17);
    p[2] = (uint8_t)(base >> 9);
    p[3] = (uint8_t)(base >> 1);
    p[4] = (uint8_t)(((base & 1) << 7) | 0x7e);
    p[5] = 0;
}

static int ts_write_packet(uint16_t pid, unsigned int *cc, int payload_start,
                           const uint8_t *payload, size_t payload_len,
                           int pcr_valid, uint64_t pcr90) {
    uint8_t pkt[188];
    size_t pos = 4;
    int use_adaptation = pcr_valid || payload_len < 184;
    int adaptation_len = 0;

    memset(pkt, 0xff, sizeof(pkt));
    pkt[0] = 0x47;
    pkt[1] = (uint8_t)(((payload_start ? 0x40 : 0x00) | ((pid >> 8) & 0x1f)));
    pkt[2] = (uint8_t)(pid & 0xff);
    pkt[3] = (uint8_t)((use_adaptation ? 0x30 : 0x10) | (*cc & 0x0f));
    *cc = (*cc + 1) & 0x0f;

    if (use_adaptation) {
        adaptation_len = pcr_valid ? 7 : 0;
        size_t payload_cap = 184u - 1u - (size_t)adaptation_len;
        if (payload_len < payload_cap) {
            adaptation_len += (int)(payload_cap - payload_len);
        }
        pkt[pos++] = (uint8_t)adaptation_len;
        if (adaptation_len > 0) {
            pkt[pos++] = pcr_valid ? 0x10 : 0x00;
            if (pcr_valid) {
                put_pcr(&pkt[pos], pcr90);
                pos += 6;
            }
            while (pos < 4u + 1u + (size_t)adaptation_len) {
                pkt[pos++] = 0xff;
            }
        }
    }

    if (payload_len > sizeof(pkt) - pos) {
        payload_len = sizeof(pkt) - pos;
    }
    memcpy(&pkt[pos], payload, payload_len);
    return write_all(STDOUT_FILENO, pkt, sizeof(pkt));
}

static int ts_write_section(uint16_t pid, unsigned int *cc,
                            const uint8_t *section, size_t section_len) {
    uint8_t payload[184];
    if (section_len + 1 > sizeof(payload)) {
        return -1;
    }
    payload[0] = 0x00;
    memcpy(&payload[1], section, section_len);
    return ts_write_packet(pid, cc, 1, payload, section_len + 1, 0, 0);
}

static int ts_write_pat(ts_mux *mux) {
    uint8_t section[16];
    uint16_t pmt_pid = 0x0100;
    uint32_t crc;
    memset(section, 0, sizeof(section));
    section[0] = 0x00;
    section[1] = 0xb0;
    section[2] = 0x0d;
    section[3] = 0x00;
    section[4] = 0x01;
    section[5] = 0xc1;
    section[6] = 0x00;
    section[7] = 0x00;
    section[8] = 0x00;
    section[9] = 0x01;
    section[10] = (uint8_t)(0xe0 | ((pmt_pid >> 8) & 0x1f));
    section[11] = (uint8_t)(pmt_pid & 0xff);
    crc = mpeg_crc32(section, 12);
    section[12] = (uint8_t)(crc >> 24);
    section[13] = (uint8_t)(crc >> 16);
    section[14] = (uint8_t)(crc >> 8);
    section[15] = (uint8_t)crc;
    return ts_write_section(0x0000, &mux->pat_cc, section, sizeof(section));
}

static int ts_write_pmt(ts_mux *mux) {
    uint8_t section[21];
    uint16_t pcr_pid = 0x0101;
    uint16_t video_pid = 0x0101;
    uint32_t crc;
    memset(section, 0, sizeof(section));
    section[0] = 0x02;
    section[1] = 0xb0;
    section[2] = 0x12;
    section[3] = 0x00;
    section[4] = 0x01;
    section[5] = 0xc1;
    section[6] = 0x00;
    section[7] = 0x00;
    section[8] = (uint8_t)(0xe0 | ((pcr_pid >> 8) & 0x1f));
    section[9] = (uint8_t)(pcr_pid & 0xff);
    section[10] = 0xf0;
    section[11] = 0x00;
    section[12] = 0x1b;
    section[13] = (uint8_t)(0xe0 | ((video_pid >> 8) & 0x1f));
    section[14] = (uint8_t)(video_pid & 0xff);
    section[15] = 0xf0;
    section[16] = 0x00;
    crc = mpeg_crc32(section, 17);
    section[17] = (uint8_t)(crc >> 24);
    section[18] = (uint8_t)(crc >> 16);
    section[19] = (uint8_t)(crc >> 8);
    section[20] = (uint8_t)crc;
    return ts_write_section(0x0100, &mux->pmt_cc, section, sizeof(section));
}

static int ts_write_video(ts_mux *mux, const uint8_t *access_unit, size_t len,
                          uint64_t pts90) {
    uint8_t pes_header[14];
    uint8_t stack_buf[4096];
    uint8_t *pes = stack_buf;
    size_t pes_len = sizeof(pes_header) + len;
    size_t offset = 0;
    int first = 1;

    pes_header[0] = 0x00;
    pes_header[1] = 0x00;
    pes_header[2] = 0x01;
    pes_header[3] = 0xe0;
    pes_header[4] = 0x00;
    pes_header[5] = 0x00;
    pes_header[6] = 0x80;
    pes_header[7] = 0x80;
    pes_header[8] = 0x05;
    put_pts(&pes_header[9], pts90);

    if (pes_len > sizeof(stack_buf)) {
        pes = (uint8_t *)malloc(pes_len);
        if (!pes) {
            return -1;
        }
    }
    memcpy(pes, pes_header, sizeof(pes_header));
    memcpy(pes + sizeof(pes_header), access_unit, len);

    while (offset < pes_len) {
        size_t remaining = pes_len - offset;
        size_t max_payload = first ? 176 : 184;
        size_t chunk = remaining < max_payload ? remaining : max_payload;
        int wr = ts_write_packet(
            0x0101,
            &mux->video_cc,
            first,
            pes + offset,
            chunk,
            first,
            pts90);
        if (wr <= 0) {
            if (pes != stack_buf) {
                free(pes);
            }
            return wr;
        }
        offset += chunk;
        first = 0;
    }

    if (pes != stack_buf) {
        free(pes);
    }
    return 1;
}

static int gl_init(void) {
    GLXFBConfig *configs = NULL;
    int count = 0;
    int attribs[] = {
        GLX_DRAWABLE_TYPE,
        GLX_PIXMAP_BIT | GLX_WINDOW_BIT,
        GLX_BIND_TO_TEXTURE_RGBA_EXT,
        1,
        GLX_BIND_TO_TEXTURE_TARGETS_EXT,
        GLX_TEXTURE_2D_BIT_EXT,
        None,
    };

    g_gl.display = XOpenDisplay(NULL);
    if (!g_gl.display) {
        fprintf(stderr, "Unable to open X display\n");
        return 0;
    }

    configs = glXChooseFBConfig(g_gl.display, DefaultScreen(g_gl.display), attribs, &count);
    if (!configs || count <= 0) {
        fprintf(stderr, "Unable to choose GLX FB config\n");
        return 0;
    }
    g_gl.fb_config = configs[0];

    g_gl.context = glXCreateNewContext(g_gl.display, g_gl.fb_config, GLX_RGBA_TYPE, None, True);
    if (!g_gl.context) {
        fprintf(stderr, "Unable to create GLX context\n");
        XFree(configs);
        return 0;
    }

    g_gl.pixmap = XCreatePixmap(
        g_gl.display,
        XDefaultRootWindow(g_gl.display),
        1,
        1,
        (unsigned int)DisplayPlanes(g_gl.display, XDefaultScreen(g_gl.display)));
    if (!g_gl.pixmap) {
        fprintf(stderr, "Unable to create GLX backing pixmap\n");
        XFree(configs);
        return 0;
    }

    g_gl.glx_pixmap = glXCreatePixmap(g_gl.display, g_gl.fb_config, g_gl.pixmap, NULL);
    if (!g_gl.glx_pixmap) {
        fprintf(stderr, "Unable to create GLX pixmap\n");
        XFree(configs);
        return 0;
    }

    if (!glXMakeCurrent(g_gl.display, g_gl.glx_pixmap, g_gl.context)) {
        fprintf(stderr, "Unable to make GLX context current\n");
        XFree(configs);
        return 0;
    }

    XFree(configs);
    return 1;
}

static void gl_cleanup(void) {
    if (!g_gl.display) {
        return;
    }
    glXMakeCurrent(g_gl.display, None, NULL);
    if (g_gl.context) {
        glXDestroyContext(g_gl.display, g_gl.context);
    }
    if (g_gl.glx_pixmap) {
        glXDestroyPixmap(g_gl.display, g_gl.glx_pixmap);
    }
    if (g_gl.pixmap) {
        XFreePixmap(g_gl.display, g_gl.pixmap);
    }
    XCloseDisplay(g_gl.display);
    memset(&g_gl, 0, sizeof(g_gl));
}

static void fail_fbc(NVFBC_API_FUNCTION_LIST *fn, NVFBC_SESSION_HANDLE handle,
                     const char *what) {
    const char *detail = handle ? fn->nvFBCGetLastErrorStr(handle) : NULL;
    fprintf(stderr, "%s failed%s%s\n", what, detail ? ": " : "", detail ? detail : "");
}

static NVENCSTATUS validate_encode_guid(void *encoder, GUID encode_guid) {
    unsigned int count = 0, returned = 0;
    GUID *guids = NULL;
    NVENCSTATUS status = g_enc.nvEncGetEncodeGUIDCount(encoder, &count);
    if (status != NV_ENC_SUCCESS || count == 0) {
        fprintf(stderr, "Failed to query supported encode GUID count, status=%d\n", status);
        return status;
    }

    guids = (GUID *)calloc(count, sizeof(GUID));
    if (!guids) {
        return NV_ENC_ERR_OUT_OF_MEMORY;
    }
    status = g_enc.nvEncGetEncodeGUIDs(encoder, guids, count, &returned);
    if (status == NV_ENC_SUCCESS) {
        for (unsigned int i = 0; i < returned; i++) {
            if (!memcmp(&encode_guid, &guids[i], sizeof(GUID))) {
                free(guids);
                return NV_ENC_SUCCESS;
            }
        }
        status = NV_ENC_ERR_UNSUPPORTED_PARAM;
    }
    free(guids);
    fprintf(stderr, "Requested encode GUID is not supported, status=%d\n", status);
    return status;
}

static int encode_cap(void *encoder, GUID encode_guid, NV_ENC_CAPS cap) {
    NV_ENC_CAPS_PARAM params;
    int value = 0;
    memset(&params, 0, sizeof(params));
    params.version = NV_ENC_CAPS_PARAM_VER;
    params.capsToQuery = cap;
    if (g_enc.nvEncGetEncodeCaps(encoder, encode_guid, &params, &value) != NV_ENC_SUCCESS) {
        return 0;
    }
    return value;
}

static void apply_low_latency_config(NV_ENC_CONFIG *cfg, const stream_options *opts,
                                     GUID profile, void *encoder, GUID encode_guid) {
    unsigned int fps = opts->fps ? opts->fps : 30;
    unsigned int gop = opts->gop ? opts->gop : fps;
    unsigned int max_bitrate = opts->max_bitrate ? opts->max_bitrate : opts->bitrate;
    unsigned int vbv_frames = opts->vbv_frames ? opts->vbv_frames : 1;
    unsigned int frame_bits = opts->bitrate / fps;

    if (frame_bits < 100000) {
        frame_bits = 100000;
    }

    cfg->profileGUID = profile;
    cfg->gopLength = gop;
    cfg->frameIntervalP = 1;
    cfg->rcParams.version = NV_ENC_RC_PARAMS_VER;
    cfg->rcParams.rateControlMode = NV_ENC_PARAMS_RC_CBR_LOWDELAY_HQ;
    cfg->rcParams.averageBitRate = opts->bitrate;
    cfg->rcParams.maxBitRate = max_bitrate;
    cfg->rcParams.vbvBufferSize = frame_bits * vbv_frames;
    cfg->rcParams.vbvInitialDelay = frame_bits;
    cfg->rcParams.zeroReorderDelay = 1;
    cfg->rcParams.enableLookahead = 0;
    cfg->rcParams.enableAQ = 1;
    cfg->rcParams.aqStrength = 8;
    if (encode_cap(encoder, encode_guid, NV_ENC_CAPS_SUPPORT_TEMPORAL_AQ) > 0) {
        cfg->rcParams.enableTemporalAQ = 1;
    }

    cfg->encodeCodecConfig.h264Config.level = NV_ENC_LEVEL_AUTOSELECT;
    cfg->encodeCodecConfig.h264Config.idrPeriod = gop;
    cfg->encodeCodecConfig.h264Config.repeatSPSPPS = 1;
    cfg->encodeCodecConfig.h264Config.outputAUD = 1;
}

static int parse_args(int argc, char **argv, stream_options *opts) {
    static struct option longopts[] = {
        {"frames", required_argument, NULL, 'f'},
        {"size", required_argument, NULL, 's'},
        {"fps", required_argument, NULL, 'r'},
        {"bitrate", required_argument, NULL, 'b'},
        {"max-bitrate", required_argument, NULL, 'm'},
        {"vbv-frames", required_argument, NULL, 'v'},
        {"gop", required_argument, NULL, 'g'},
        {"timeout-ms", required_argument, NULL, 't'},
        {"sampling-ms", required_argument, NULL, 1000},
        {"preset", required_argument, NULL, 'p'},
        {"profile", required_argument, NULL, 1001},
        {"mux", required_argument, NULL, 1004},
        {"no-cursor", no_argument, NULL, 1002},
        {"no-push", no_argument, NULL, 1003},
        {"quiet", no_argument, NULL, 'q'},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };
    int opt;

    memset(opts, 0, sizeof(*opts));
    opts->frames = 0;
    opts->fps = 30;
    opts->bitrate = 12000000;
    opts->vbv_frames = 1;
    opts->cursor = 1;
    opts->push_model = 1;
    opts->preset_name = "llhp";
    opts->profile = PROFILE_BASELINE;

    while ((opt = getopt_long(argc, argv, "hf:s:r:b:m:v:g:t:p:q", longopts, NULL)) != -1) {
        switch (opt) {
        case 'f':
            if (!parse_u32(optarg, 0, 100000000, &opts->frames)) {
                return 0;
            }
            break;
        case 's':
            if (!parse_size(optarg, &opts->size)) {
                return 0;
            }
            break;
        case 'r':
            if (!parse_u32(optarg, 1, 240, &opts->fps)) {
                return 0;
            }
            break;
        case 'b':
            if (!parse_u32(optarg, 100000, 1000000000, &opts->bitrate)) {
                return 0;
            }
            break;
        case 'm':
            if (!parse_u32(optarg, 100000, 1000000000, &opts->max_bitrate)) {
                return 0;
            }
            break;
        case 'v':
            if (!parse_u32(optarg, 1, 60, &opts->vbv_frames)) {
                return 0;
            }
            break;
        case 'g':
            if (!parse_u32(optarg, 1, 10000, &opts->gop)) {
                return 0;
            }
            break;
        case 't':
            if (!parse_u32(optarg, 1, 10000, &opts->timeout_ms)) {
                return 0;
            }
            break;
        case 1000:
            if (!parse_u32(optarg, 1, 10000, &opts->sampling_ms)) {
                return 0;
            }
            break;
        case 'p':
            opts->preset_name = optarg;
            break;
        case 1001:
            if (!parse_profile(optarg, &opts->profile)) {
                return 0;
            }
            break;
        case 1004:
            if (!strcasecmp(optarg, "mpegts") || !strcasecmp(optarg, "ts")) {
                opts->mpegts = 1;
            } else if (!strcasecmp(optarg, "h264") || !strcasecmp(optarg, "raw")) {
                opts->mpegts = 0;
            } else {
                return 0;
            }
            break;
        case 1002:
            opts->cursor = 0;
            break;
        case 1003:
            opts->push_model = 0;
            break;
        case 'q':
            opts->quiet = 1;
            break;
        case 'h':
            usage(argv[0]);
            exit(0);
        default:
            return 0;
        }
    }

    if (opts->max_bitrate == 0) {
        opts->max_bitrate = opts->bitrate;
    }
    if (opts->gop == 0) {
        opts->gop = opts->fps;
    }
    if (opts->timeout_ms == 0) {
        opts->timeout_ms = 1000 / opts->fps;
        if (opts->timeout_ms == 0) {
            opts->timeout_ms = 1;
        }
    }
    if (opts->sampling_ms == 0) {
        opts->sampling_ms = 1000 / opts->fps;
        if (opts->sampling_ms == 0) {
            opts->sampling_ms = 1;
        }
    }
    return 1;
}

int main(int argc, char **argv) {
    stream_options opts;
    void *lib_fbc = NULL;
    void *lib_enc = NULL;
    PNVFBCCREATEINSTANCE NvFBCCreateInstance = NULL;
    PFNNVENCODEAPICREATEINSTANCEPROC NvEncodeAPICreateInstance = NULL;
    NVFBC_API_FUNCTION_LIST fbc;
    NVFBC_SESSION_HANDLE fbc_handle = 0;
    NVFBC_CREATE_HANDLE_PARAMS create_handle;
    NVFBC_DESTROY_HANDLE_PARAMS destroy_handle;
    NVFBC_DESTROY_CAPTURE_SESSION_PARAMS destroy_capture;
    NVFBCSTATUS fbc_status;
    NVENCSTATUS enc_status;
    void *encoder = NULL;
    NV_ENC_REGISTERED_PTR registered_resource = NULL;
    NV_ENC_OUTPUT_PTR output_buffer = NULL;
    NV_ENC_INPUT_PTR input_buffer = NULL;
    int have_capture = 0;
    int ret = 1;

    if (!parse_args(argc, argv, &opts)) {
        usage(argv[0]);
        return 2;
    }

    lib_fbc = dlopen(LIB_NVFBC_NAME, RTLD_NOW);
    if (!lib_fbc) {
        fprintf(stderr, "Unable to open %s: %s\n", LIB_NVFBC_NAME, dlerror());
        goto done;
    }
    lib_enc = dlopen(LIB_ENCODEAPI_NAME, RTLD_NOW);
    if (!lib_enc) {
        fprintf(stderr, "Unable to open %s: %s\n", LIB_ENCODEAPI_NAME, dlerror());
        goto done;
    }
    if (!gl_init()) {
        goto done;
    }

    NvFBCCreateInstance = (PNVFBCCREATEINSTANCE)dlsym(lib_fbc, "NvFBCCreateInstance");
    if (!NvFBCCreateInstance) {
        fprintf(stderr, "Unable to resolve NvFBCCreateInstance\n");
        goto done;
    }
    NvEncodeAPICreateInstance =
        (PFNNVENCODEAPICREATEINSTANCEPROC)dlsym(lib_enc, "NvEncodeAPICreateInstance");
    if (!NvEncodeAPICreateInstance) {
        fprintf(stderr, "Unable to resolve NvEncodeAPICreateInstance\n");
        goto done;
    }

    memset(&fbc, 0, sizeof(fbc));
    fbc.dwVersion = NVFBC_VERSION;
    fbc_status = NvFBCCreateInstance(&fbc);
    if (fbc_status != NVFBC_SUCCESS) {
        fprintf(stderr, "NvFBCCreateInstance failed, status=%d\n", fbc_status);
        goto done;
    }

    memset(&g_enc, 0, sizeof(g_enc));
    g_enc.version = NV_ENCODE_API_FUNCTION_LIST_VER;
    enc_status = NvEncodeAPICreateInstance(&g_enc);
    if (enc_status != NV_ENC_SUCCESS) {
        fprintf(stderr, "NvEncodeAPICreateInstance failed, status=%d\n", enc_status);
        goto done;
    }

    memset(&create_handle, 0, sizeof(create_handle));
    create_handle.dwVersion = NVFBC_CREATE_HANDLE_PARAMS_VER;
    create_handle.bExternallyManagedContext = NVFBC_TRUE;
    create_handle.glxCtx = g_gl.context;
    create_handle.glxFBConfig = g_gl.fb_config;
    fbc_status = fbc.nvFBCCreateHandle(&fbc_handle, &create_handle);
    if (fbc_status != NVFBC_SUCCESS) {
        fail_fbc(&fbc, fbc_handle, "NvFBCCreateHandle");
        goto done;
    }

    NVFBC_GET_STATUS_PARAMS status;
    memset(&status, 0, sizeof(status));
    status.dwVersion = NVFBC_GET_STATUS_PARAMS_VER;
    fbc_status = fbc.nvFBCGetStatus(fbc_handle, &status);
    if (fbc_status != NVFBC_SUCCESS) {
        fail_fbc(&fbc, fbc_handle, "NvFBCGetStatus");
        goto done;
    }
    if (!status.bCanCreateNow) {
        fprintf(stderr, "NvFBC cannot create a capture session now\n");
        goto done;
    }

    if (opts.size.w == 0) {
        opts.size.w = status.screenSize.w;
    }
    if (opts.size.h == 0) {
        opts.size.h = status.screenSize.h;
    }
    if (opts.size.w > status.screenSize.w || opts.size.h > status.screenSize.h) {
        fprintf(stderr, "Requested size %ux%u exceeds framebuffer %ux%u\n",
                opts.size.w, opts.size.h, status.screenSize.w, status.screenSize.h);
        goto done;
    }
    opts.size.w = (opts.size.w + 3u) & ~3u;
    opts.size.h = (opts.size.h + 1u) & ~1u;

    if (!opts.quiet) {
        fprintf(stderr,
                "NvFBC NVENC streamer: %ux%u fps=%u bitrate=%u preset=%s "
                "gop=%u vbv_frames=%u cursor=%s\n",
                opts.size.w, opts.size.h, opts.fps, opts.bitrate, opts.preset_name,
                opts.gop, opts.vbv_frames, opts.cursor ? "on" : "off");
    }

    NVFBC_CREATE_CAPTURE_SESSION_PARAMS create_capture;
    memset(&create_capture, 0, sizeof(create_capture));
    create_capture.dwVersion = NVFBC_CREATE_CAPTURE_SESSION_PARAMS_VER;
    create_capture.eCaptureType = NVFBC_CAPTURE_TO_GL;
    create_capture.eTrackingType = NVFBC_TRACKING_SCREEN;
    create_capture.frameSize = opts.size;
    create_capture.bWithCursor = opts.cursor ? NVFBC_TRUE : NVFBC_FALSE;
    create_capture.bRoundFrameSize = NVFBC_TRUE;
    create_capture.bDisableAutoModesetRecovery = NVFBC_TRUE;
    create_capture.dwSamplingRateMs = opts.sampling_ms;
    create_capture.bPushModel = opts.push_model ? NVFBC_TRUE : NVFBC_FALSE;
    fbc_status = fbc.nvFBCCreateCaptureSession(fbc_handle, &create_capture);
    if (fbc_status != NVFBC_SUCCESS) {
        fail_fbc(&fbc, fbc_handle, "NvFBCCreateCaptureSession");
        goto done;
    }
    have_capture = 1;

    NVFBC_TOGL_SETUP_PARAMS setup;
    memset(&setup, 0, sizeof(setup));
    setup.dwVersion = NVFBC_TOGL_SETUP_PARAMS_VER;
    setup.eBufferFormat = NVFBC_BUFFER_FORMAT_NV12;
    fbc_status = fbc.nvFBCToGLSetUp(fbc_handle, &setup);
    if (fbc_status != NVFBC_SUCCESS) {
        fail_fbc(&fbc, fbc_handle, "NvFBCToGLSetUp");
        goto done;
    }

    NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS session_params;
    memset(&session_params, 0, sizeof(session_params));
    session_params.version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER;
    session_params.apiVersion = NVENCAPI_VERSION;
    session_params.deviceType = NV_ENC_DEVICE_TYPE_OPENGL;
    enc_status = g_enc.nvEncOpenEncodeSessionEx(&session_params, &encoder);
    if (enc_status != NV_ENC_SUCCESS) {
        fprintf(stderr, "Failed to open NVENC session, status=%d\n", enc_status);
        goto done;
    }

    GUID encode_guid = NV_ENC_CODEC_H264_GUID;
    GUID selected_preset = preset_guid(opts.preset_name);
    GUID selected_profile = profile_guid(opts.profile);
    enc_status = validate_encode_guid(encoder, encode_guid);
    if (enc_status != NV_ENC_SUCCESS) {
        goto done;
    }

    NV_ENC_PRESET_CONFIG preset_config;
    memset(&preset_config, 0, sizeof(preset_config));
    preset_config.version = NV_ENC_PRESET_CONFIG_VER;
    preset_config.presetCfg.version = NV_ENC_CONFIG_VER;
    enc_status = g_enc.nvEncGetEncodePresetConfig(
        encoder, encode_guid, selected_preset, &preset_config);
    if (enc_status != NV_ENC_SUCCESS) {
        fprintf(stderr, "Failed to get NVENC preset config, status=%d\n", enc_status);
        goto done;
    }
    apply_low_latency_config(
        &preset_config.presetCfg, &opts, selected_profile, encoder, encode_guid);

    NV_ENC_INITIALIZE_PARAMS init;
    memset(&init, 0, sizeof(init));
    init.version = NV_ENC_INITIALIZE_PARAMS_VER;
    init.encodeGUID = encode_guid;
    init.presetGUID = selected_preset;
    init.encodeConfig = &preset_config.presetCfg;
    init.encodeWidth = opts.size.w;
    init.encodeHeight = opts.size.h;
    init.darWidth = opts.size.w;
    init.darHeight = opts.size.h;
    init.frameRateNum = opts.fps;
    init.frameRateDen = 1;
    init.enablePTD = 1;
    init.enableEncodeAsync = 0;
    enc_status = g_enc.nvEncInitializeEncoder(encoder, &init);
    if (enc_status != NV_ENC_SUCCESS) {
        fprintf(stderr, "Failed to initialize NVENC, status=%d\n", enc_status);
        goto done;
    }

    NV_ENC_INPUT_RESOURCE_OPENGL_TEX tex;
    memset(&tex, 0, sizeof(tex));
    tex.texture = setup.dwTextures[0];
    tex.target = setup.dwTexTarget;

    NV_ENC_REGISTER_RESOURCE reg;
    memset(&reg, 0, sizeof(reg));
    reg.version = NV_ENC_REGISTER_RESOURCE_VER;
    reg.resourceType = NV_ENC_INPUT_RESOURCE_TYPE_OPENGL_TEX;
    reg.width = opts.size.w;
    reg.height = opts.size.h;
    reg.pitch = opts.size.w;
    reg.resourceToRegister = &tex;
    reg.bufferFormat = NV_ENC_BUFFER_FORMAT_NV12;
    enc_status = g_enc.nvEncRegisterResource(encoder, &reg);
    if (enc_status != NV_ENC_SUCCESS) {
        fprintf(stderr, "Failed to register GL texture with NVENC, status=%d\n", enc_status);
        goto done;
    }
    registered_resource = reg.registeredResource;

    NV_ENC_CREATE_BITSTREAM_BUFFER bitstream;
    memset(&bitstream, 0, sizeof(bitstream));
    bitstream.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER;
    enc_status = g_enc.nvEncCreateBitstreamBuffer(encoder, &bitstream);
    if (enc_status != NV_ENC_SUCCESS) {
        fprintf(stderr, "Failed to create NVENC bitstream buffer, status=%d\n", enc_status);
        goto done;
    }
    output_buffer = bitstream.bitstreamBuffer;

    uint64_t started_ns = now_ns();
    uint64_t frame_interval_ns = 1000000000ull / opts.fps;
    unsigned int encoded = 0;
    unsigned int new_frames = 0;
    ts_mux mux;
    memset(&mux, 0, sizeof(mux));

    for (unsigned int i = 0; opts.frames == 0 || i < opts.frames; i++) {
        NVFBC_FRAME_GRAB_INFO frame_info;
        NVFBC_TOGL_GRAB_FRAME_PARAMS grab;
        NV_ENC_MAP_INPUT_RESOURCE map;
        NV_ENC_PIC_PARAMS pic;
        NV_ENC_LOCK_BITSTREAM lock;

        memset(&frame_info, 0, sizeof(frame_info));
        memset(&grab, 0, sizeof(grab));
        grab.dwVersion = NVFBC_TOGL_GRAB_FRAME_PARAMS_VER;
        grab.dwFlags = NVFBC_TOGL_GRAB_FLAGS_NOWAIT_IF_NEW_FRAME_READY;
        grab.dwTimeoutMs = opts.timeout_ms;
        grab.pFrameGrabInfo = &frame_info;
        fbc_status = fbc.nvFBCToGLGrabFrame(fbc_handle, &grab);
        if (fbc_status == NVFBC_ERR_MUST_RECREATE) {
            fprintf(stderr, "NvFBC capture session must be recreated\n");
            break;
        }
        if (fbc_status != NVFBC_SUCCESS) {
            fail_fbc(&fbc, fbc_handle, "NvFBCToGLGrabFrame");
            goto done;
        }
        if (frame_info.bIsNewFrame) {
            new_frames++;
        }

        memset(&map, 0, sizeof(map));
        map.version = NV_ENC_MAP_INPUT_RESOURCE_VER;
        map.registeredResource = registered_resource;
        enc_status = g_enc.nvEncMapInputResource(encoder, &map);
        if (enc_status != NV_ENC_SUCCESS) {
            fprintf(stderr, "Failed to map NVENC input resource, status=%d\n", enc_status);
            goto done;
        }
        input_buffer = map.mappedResource;

        memset(&pic, 0, sizeof(pic));
        pic.version = NV_ENC_PIC_PARAMS_VER;
        pic.inputBuffer = input_buffer;
        pic.bufferFmt = map.mappedBufferFmt;
        pic.inputWidth = opts.size.w;
        pic.inputHeight = opts.size.h;
        pic.inputPitch = opts.size.w;
        pic.outputBitstream = output_buffer;
        pic.pictureStruct = NV_ENC_PIC_STRUCT_FRAME;
        pic.frameIdx = encoded;
        pic.inputTimeStamp = encoded;
        if (encoded == 0 || (opts.gop > 0 && encoded % opts.gop == 0)) {
            pic.encodePicFlags = NV_ENC_PIC_FLAG_FORCEIDR | NV_ENC_PIC_FLAG_OUTPUT_SPSPPS;
        }

        enc_status = g_enc.nvEncEncodePicture(encoder, &pic);
        if (enc_status != NV_ENC_SUCCESS) {
            fprintf(stderr, "Failed to encode frame %u, status=%d\n", encoded, enc_status);
            g_enc.nvEncUnmapInputResource(encoder, input_buffer);
            input_buffer = NULL;
            goto done;
        }

        memset(&lock, 0, sizeof(lock));
        lock.version = NV_ENC_LOCK_BITSTREAM_VER;
        lock.outputBitstream = output_buffer;
        enc_status = g_enc.nvEncLockBitstream(encoder, &lock);
        if (enc_status != NV_ENC_SUCCESS) {
            fprintf(stderr, "Failed to lock NVENC bitstream, status=%d\n", enc_status);
            g_enc.nvEncUnmapInputResource(encoder, input_buffer);
            input_buffer = NULL;
            goto done;
        }

        int wr;
        if (opts.mpegts) {
            uint64_t pts90 = ((uint64_t)encoded * 90000ull) / opts.fps;
            if (encoded % opts.gop == 0) {
                wr = ts_write_pat(&mux);
                if (wr > 0) {
                    wr = ts_write_pmt(&mux);
                }
                if (wr <= 0) {
                    g_enc.nvEncUnlockBitstream(encoder, output_buffer);
                    g_enc.nvEncUnmapInputResource(encoder, input_buffer);
                    input_buffer = NULL;
                    ret = wr == 0 ? 0 : 1;
                    goto done;
                }
            }
            wr = ts_write_video(
                &mux,
                (const uint8_t *)lock.bitstreamBufferPtr,
                lock.bitstreamSizeInBytes,
                pts90);
        } else {
            wr = write_all(STDOUT_FILENO, lock.bitstreamBufferPtr,
                           lock.bitstreamSizeInBytes);
        }
        enc_status = g_enc.nvEncUnlockBitstream(encoder, output_buffer);
        if (enc_status != NV_ENC_SUCCESS) {
            fprintf(stderr, "Failed to unlock NVENC bitstream, status=%d\n", enc_status);
            g_enc.nvEncUnmapInputResource(encoder, input_buffer);
            input_buffer = NULL;
            goto done;
        }
        if (wr == 0) {
            ret = 0;
            goto done;
        }
        if (wr < 0) {
            goto done;
        }

        enc_status = g_enc.nvEncUnmapInputResource(encoder, input_buffer);
        input_buffer = NULL;
        if (enc_status != NV_ENC_SUCCESS) {
            fprintf(stderr, "Failed to unmap NVENC input resource, status=%d\n", enc_status);
            goto done;
        }

        encoded++;
        if (!opts.quiet && (encoded == 1 || encoded % (opts.fps * 5) == 0)) {
            double elapsed = (double)(now_ns() - started_ns) / 1000000000.0;
            fprintf(stderr, "encoded=%u new=%u elapsed=%.2fs fps=%.2f\n",
                    encoded, new_frames, elapsed, (double)encoded / elapsed);
        }

        uint64_t target_ns = started_ns + (uint64_t)encoded * frame_interval_ns;
        uint64_t current = now_ns();
        if (target_ns < current) {
            started_ns = current - (uint64_t)encoded * frame_interval_ns;
        } else {
            sleep_until_ns(target_ns);
        }
    }

    ret = 0;

done:
    if (encoder) {
        NV_ENC_PIC_PARAMS eos;
        memset(&eos, 0, sizeof(eos));
        eos.version = NV_ENC_PIC_PARAMS_VER;
        eos.encodePicFlags = NV_ENC_PIC_FLAG_EOS;
        g_enc.nvEncEncodePicture(encoder, &eos);
    }
    if (input_buffer && encoder) {
        g_enc.nvEncUnmapInputResource(encoder, input_buffer);
    }
    if (output_buffer && encoder) {
        g_enc.nvEncDestroyBitstreamBuffer(encoder, output_buffer);
    }
    if (registered_resource && encoder) {
        g_enc.nvEncUnregisterResource(encoder, registered_resource);
    }
    if (encoder) {
        g_enc.nvEncDestroyEncoder(encoder);
    }
    if (have_capture && fbc_handle) {
        memset(&destroy_capture, 0, sizeof(destroy_capture));
        destroy_capture.dwVersion = NVFBC_DESTROY_CAPTURE_SESSION_PARAMS_VER;
        fbc.nvFBCDestroyCaptureSession(fbc_handle, &destroy_capture);
    }
    if (fbc_handle) {
        memset(&destroy_handle, 0, sizeof(destroy_handle));
        destroy_handle.dwVersion = NVFBC_DESTROY_HANDLE_PARAMS_VER;
        fbc.nvFBCDestroyHandle(fbc_handle, &destroy_handle);
    }
    gl_cleanup();
    if (lib_enc) {
        dlclose(lib_enc);
    }
    if (lib_fbc) {
        dlclose(lib_fbc);
    }
    return ret;
}

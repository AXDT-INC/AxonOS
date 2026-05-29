// Tiny NvFBC capture benchmark.
//
// Build with:
//   gcc -O2 -Wall -I/path/to/Capture_Linux_v7.1.9/NvFBC/inc tools/nvfbc_bench.c -ldl -o /tmp/nvfbc_bench
//
// This file intentionally depends on a user-provided NVIDIA Capture SDK and
// does not vendor SDK headers into the repository.

#include <NvFBC.h>

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define LIB_NVFBC_NAME "libnvidia-fbc.so.1"

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void fail_fbc(NVFBC_API_FUNCTION_LIST *fn, NVFBC_SESSION_HANDLE handle, const char *what) {
    const char *detail = handle ? fn->nvFBCGetLastErrorStr(handle) : NULL;
    fprintf(stderr, "%s failed%s%s\n", what, detail ? ": " : "", detail ? detail : "");
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

static NVFBC_BUFFER_FORMAT parse_format(const char *raw) {
    if (!strcmp(raw, "rgb")) return NVFBC_BUFFER_FORMAT_RGB;
    if (!strcmp(raw, "argb")) return NVFBC_BUFFER_FORMAT_ARGB;
    if (!strcmp(raw, "rgba")) return NVFBC_BUFFER_FORMAT_RGBA;
    if (!strcmp(raw, "bgra")) return NVFBC_BUFFER_FORMAT_BGRA;
    if (!strcmp(raw, "nv12")) return NVFBC_BUFFER_FORMAT_NV12;
    return NVFBC_BUFFER_FORMAT_NV12;
}

int main(int argc, char **argv) {
    unsigned int frames = 300;
    unsigned int timeout_ms = 1000;
    uint32_t sampling_ms = 33;
    int with_cursor = 0;
    int push_model = 0;
    int force_refresh = 1;
    int nowait = 0;
    int if_new = 0;
    int raw_stdout = 0;
    double fps = 30.0;
    NVFBC_SIZE size = {0, 0};
    NVFBC_BUFFER_FORMAT format = NVFBC_BUFFER_FORMAT_NV12;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--frames") && i + 1 < argc) {
            frames = (unsigned int)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--size") && i + 1 < argc) {
            if (!parse_size(argv[++i], &size)) {
                fprintf(stderr, "invalid --size, expected WIDTHxHEIGHT\n");
                return 2;
            }
        } else if (!strcmp(argv[i], "--format") && i + 1 < argc) {
            format = parse_format(argv[++i]);
        } else if (!strcmp(argv[i], "--timeout-ms") && i + 1 < argc) {
            timeout_ms = (unsigned int)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--sampling-ms") && i + 1 < argc) {
            sampling_ms = (uint32_t)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--fps") && i + 1 < argc) {
            fps = atof(argv[++i]);
            if (fps <= 0.0) fps = 30.0;
        } else if (!strcmp(argv[i], "--cursor")) {
            with_cursor = 1;
        } else if (!strcmp(argv[i], "--push")) {
            push_model = 1;
        } else if (!strcmp(argv[i], "--raw-stdout")) {
            raw_stdout = 1;
        } else if (!strcmp(argv[i], "--nowait")) {
            nowait = 1;
            force_refresh = 0;
            if_new = 0;
        } else if (!strcmp(argv[i], "--if-new")) {
            if_new = 1;
            force_refresh = 0;
            nowait = 0;
        } else if (!strcmp(argv[i], "--no-force-refresh")) {
            force_refresh = 0;
        } else {
            fprintf(stderr,
                    "usage: %s [--frames N] [--size WxH] [--format nv12|rgb|argb|rgba|bgra]\n"
                    "          [--sampling-ms N] [--timeout-ms N] [--fps N]\n"
                    "          [--cursor] [--push] [--raw-stdout]\n"
                    "          [--nowait] [--if-new]\n"
                    "          [--no-force-refresh]\n",
                    argv[0]);
            return 2;
        }
    }
    if (raw_stdout && !nowait && !if_new && force_refresh) {
        nowait = 1;
        force_refresh = 0;
    }
    if (!raw_stdout && frames == 0) frames = 1;

    void *lib = dlopen(LIB_NVFBC_NAME, RTLD_NOW);
    if (!lib) {
        fprintf(stderr, "unable to open %s: %s\n", LIB_NVFBC_NAME, dlerror());
        return 1;
    }

    PNVFBCCREATEINSTANCE create_instance =
        (PNVFBCCREATEINSTANCE)dlsym(lib, "NvFBCCreateInstance");
    if (!create_instance) {
        fprintf(stderr, "unable to resolve NvFBCCreateInstance\n");
        return 1;
    }

    NVFBC_API_FUNCTION_LIST fn;
    memset(&fn, 0, sizeof(fn));
    fn.dwVersion = NVFBC_VERSION;
    if (create_instance(&fn) != NVFBC_SUCCESS) {
        fprintf(stderr, "NvFBCCreateInstance failed\n");
        return 1;
    }

    NVFBC_SESSION_HANDLE handle = 0;
    NVFBC_CREATE_HANDLE_PARAMS create_handle;
    memset(&create_handle, 0, sizeof(create_handle));
    create_handle.dwVersion = NVFBC_CREATE_HANDLE_PARAMS_VER;
    if (fn.nvFBCCreateHandle(&handle, &create_handle) != NVFBC_SUCCESS) {
        fail_fbc(&fn, handle, "NvFBCCreateHandle");
        return 1;
    }

    NVFBC_GET_STATUS_PARAMS status;
    memset(&status, 0, sizeof(status));
    status.dwVersion = NVFBC_GET_STATUS_PARAMS_VER;
    if (fn.nvFBCGetStatus(handle, &status) != NVFBC_SUCCESS) {
        fail_fbc(&fn, handle, "NvFBCGetStatus");
        return 1;
    }
    fprintf(stderr, "NvFBC: supported=%u can_create=%u framebuffer=%ux%u\n",
            status.bIsCapturePossible, status.bCanCreateNow,
            status.screenSize.w, status.screenSize.h);
    if (!status.bCanCreateNow) {
        fprintf(stderr, "NvFBC cannot create a capture session right now\n");
        return 1;
    }

    NVFBC_CREATE_CAPTURE_SESSION_PARAMS create_capture;
    memset(&create_capture, 0, sizeof(create_capture));
    create_capture.dwVersion = NVFBC_CREATE_CAPTURE_SESSION_PARAMS_VER;
    create_capture.eCaptureType = NVFBC_CAPTURE_TO_SYS;
    create_capture.eTrackingType = NVFBC_TRACKING_SCREEN;
    create_capture.frameSize = size;
    create_capture.bWithCursor = with_cursor ? NVFBC_TRUE : NVFBC_FALSE;
    create_capture.bRoundFrameSize = NVFBC_TRUE;
    create_capture.dwSamplingRateMs = sampling_ms;
    create_capture.bPushModel = push_model ? NVFBC_TRUE : NVFBC_FALSE;
    if (fn.nvFBCCreateCaptureSession(handle, &create_capture) != NVFBC_SUCCESS) {
        fail_fbc(&fn, handle, "NvFBCCreateCaptureSession");
        return 1;
    }

    void *buffer = NULL;
    NVFBC_TOSYS_SETUP_PARAMS setup;
    memset(&setup, 0, sizeof(setup));
    setup.dwVersion = NVFBC_TOSYS_SETUP_PARAMS_VER;
    setup.eBufferFormat = format;
    setup.ppBuffer = &buffer;
    if (fn.nvFBCToSysSetUp(handle, &setup) != NVFBC_SUCCESS) {
        fail_fbc(&fn, handle, "NvFBCToSysSetUp");
        return 1;
    }

    uint64_t total_ns = 0, min_ns = UINT64_MAX, max_ns = 0;
    unsigned int new_frames = 0;
    volatile unsigned char sink = 0;
    NVFBC_FRAME_GRAB_INFO last_info;
    memset(&last_info, 0, sizeof(last_info));

    uint64_t bench_start = now_ns();
    uint64_t frame_interval_ns = (uint64_t)(1000000000.0 / fps);
    for (unsigned int i = 0; frames == 0 || i < frames; i++) {
        NVFBC_FRAME_GRAB_INFO info;
        NVFBC_TOSYS_GRAB_FRAME_PARAMS grab;
        memset(&info, 0, sizeof(info));
        memset(&grab, 0, sizeof(grab));
        grab.dwVersion = NVFBC_TOSYS_GRAB_FRAME_PARAMS_VER;
        if (force_refresh) {
            grab.dwFlags = NVFBC_TOSYS_GRAB_FLAGS_FORCE_REFRESH;
        } else if (nowait) {
            grab.dwFlags = NVFBC_TOSYS_GRAB_FLAGS_NOWAIT;
        } else if (if_new) {
            grab.dwFlags = NVFBC_TOSYS_GRAB_FLAGS_NOWAIT_IF_NEW_FRAME_READY;
        } else {
            grab.dwFlags = NVFBC_TOSYS_GRAB_FLAGS_NOFLAGS;
        }
        grab.dwTimeoutMs = timeout_ms;
        grab.pFrameGrabInfo = &info;

        uint64_t t1 = now_ns();
        NVFBCSTATUS st = fn.nvFBCToSysGrabFrame(handle, &grab);
        uint64_t dt = now_ns() - t1;
        if (st != NVFBC_SUCCESS) {
            fail_fbc(&fn, handle, "NvFBCToSysGrabFrame");
            return 1;
        }
        if (buffer && info.dwByteSize > 0) {
            sink ^= ((unsigned char *)buffer)[0];
        }
        if (raw_stdout && buffer && info.dwByteSize > 0) {
            size_t remaining = info.dwByteSize;
            const unsigned char *p = (const unsigned char *)buffer;
            while (remaining > 0) {
                ssize_t n = write(STDOUT_FILENO, p, remaining);
                if (n <= 0) {
                    perror("write");
                    return 1;
                }
                p += (size_t)n;
                remaining -= (size_t)n;
            }
        }
        if (info.bIsNewFrame) new_frames++;
        total_ns += dt;
        if (dt < min_ns) min_ns = dt;
        if (dt > max_ns) max_ns = dt;
        last_info = info;
        if (raw_stdout && frame_interval_ns > 0) {
            uint64_t target_ns = bench_start + ((uint64_t)i + 1) * frame_interval_ns;
            uint64_t tnow = now_ns();
            if (target_ns > tnow) {
                uint64_t sleep_ns = target_ns - tnow;
                struct timespec req;
                req.tv_sec = (time_t)(sleep_ns / 1000000000ull);
                req.tv_nsec = (long)(sleep_ns % 1000000000ull);
                nanosleep(&req, NULL);
            }
        }
    }
    uint64_t bench_total_ns = now_ns() - bench_start;

    FILE *summary = raw_stdout ? stderr : stdout;
    fprintf(summary, "frames=%u new=%u size=%ux%u bytes=%u avg_grab_ms=%.3f min_ms=%.3f max_ms=%.3f wall_fps=%.2f sink=%u\n",
           frames, new_frames, last_info.dwWidth, last_info.dwHeight,
           last_info.dwByteSize,
           (double)total_ns / (double)frames / 1000000.0,
           (double)min_ns / 1000000.0,
           (double)max_ns / 1000000.0,
           (double)frames * 1000000000.0 / (double)bench_total_ns,
           (unsigned int)sink);

    NVFBC_DESTROY_CAPTURE_SESSION_PARAMS destroy_capture;
    memset(&destroy_capture, 0, sizeof(destroy_capture));
    destroy_capture.dwVersion = NVFBC_DESTROY_CAPTURE_SESSION_PARAMS_VER;
    fn.nvFBCDestroyCaptureSession(handle, &destroy_capture);

    NVFBC_DESTROY_HANDLE_PARAMS destroy_handle;
    memset(&destroy_handle, 0, sizeof(destroy_handle));
    destroy_handle.dwVersion = NVFBC_DESTROY_HANDLE_PARAMS_VER;
    fn.nvFBCDestroyHandle(handle, &destroy_handle);
    dlclose(lib);
    return 0;
}

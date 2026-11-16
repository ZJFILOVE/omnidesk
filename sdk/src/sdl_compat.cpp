#include <SDL.h>
#include <dlfcn.h>
#include <cstdio>
#include <cstdlib>

namespace {

using SdlInitFn = int (*)(Uint32);

void applyDesktopWindowHints() {
    static bool applied = false;
    if (applied) {
        return;
    }
    applied = true;

    const char* wayland_display = std::getenv("WAYLAND_DISPLAY");
    const char* sdl_driver = std::getenv("SDL_VIDEODRIVER");
    if (wayland_display && wayland_display[0] != '\0' &&
        (!sdl_driver || sdl_driver[0] == '\0')) {
        SDL_SetHintWithPriority(SDL_HINT_VIDEODRIVER, "wayland,x11", SDL_HINT_DEFAULT);
    }

    // Hyprland supports xdg-decoration server-side borders natively; disabling libdecor
    // prevents CSD titlebar/buffer-size desync when toggling fullscreen or tiling.
#ifdef SDL_HINT_VIDEO_WAYLAND_ALLOW_LIBDECOR
    SDL_SetHintWithPriority(SDL_HINT_VIDEO_WAYLAND_ALLOW_LIBDECOR, "0", SDL_HINT_DEFAULT);
#else
    SDL_SetHintWithPriority("SDL_VIDEO_WAYLAND_ALLOW_LIBDECOR", "0", SDL_HINT_DEFAULT);
#endif
#ifdef SDL_HINT_VIDEO_WAYLAND_SCALE_TO_DISPLAY
    SDL_SetHintWithPriority(SDL_HINT_VIDEO_WAYLAND_SCALE_TO_DISPLAY, "0", SDL_HINT_DEFAULT);
#endif
    // Prevent fullscreen windows from auto-iconifying when Hyprland focus moves across tiles/monitors.
#ifdef SDL_HINT_VIDEO_MINIMIZE_ON_FOCUS_LOSS
    SDL_SetHintWithPriority(SDL_HINT_VIDEO_MINIMIZE_ON_FOCUS_LOSS, "0", SDL_HINT_DEFAULT);
#else
    SDL_SetHintWithPriority("SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS", "0", SDL_HINT_DEFAULT);
#endif
    SDL_SetHintWithPriority("SDL_VIDEO_WAYLAND_WMCLASS", "omnidesk", SDL_HINT_DEFAULT);
    SDL_SetHintWithPriority("SDL_VIDEO_X11_WMCLASS", "omnidesk", SDL_HINT_DEFAULT);
}

SdlInitFn resolveRealSdlInit() {
    if (void* sym = dlsym(RTLD_NEXT, "SDL_Init")) {
        return reinterpret_cast<SdlInitFn>(sym);
    }
    if (void* sym = dlsym(RTLD_DEFAULT, "SDL_Init")) {
        return reinterpret_cast<SdlInitFn>(sym);
    }
    if (void* handle = dlopen("libSDL2-2.0.so.0", RTLD_LAZY | RTLD_NOLOAD)) {
        if (void* sym = dlsym(handle, "SDL_Init")) {
            return reinterpret_cast<SdlInitFn>(sym);
        }
    }
    return nullptr;
}

} // namespace

#if defined(__GNUC__) || defined(__clang__)
extern "C" __attribute__((used)) int __wrap_SDL_Init(Uint32 flags) {
#else
extern "C" int __wrap_SDL_Init(Uint32 flags) {
#endif
    SdlInitFn real_init = resolveRealSdlInit();
    if (!real_init) {
        SDL_SetError("Unable to resolve real SDL_Init");
        return -1;
    }

    if ((flags & SDL_INIT_VIDEO) != 0) {
        applyDesktopWindowHints();
    }

    int rc = real_init(flags);
    if (rc == 0) {
        return 0;
    }

    // In cleanroom/sandbox/container Linux environments without UDEV or /dev/input,
    // Borealis SDLInputManager requests SDL_INIT_HAPTIC alongside joystick/gamecontroller,
    // which fails with "Could not initialize UDEV" and aborts startup via brls::fatal().
    constexpr Uint32 kOptionalInputFlags =
        SDL_INIT_HAPTIC | SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER;

    if ((flags & SDL_INIT_HAPTIC) != 0) {
        const char* first_err = SDL_GetError();
        std::fprintf(stderr,
                     "[omnidesk-sdl] SDL_Init(0x%x) failed (%s); retrying without SDL_INIT_HAPTIC\n",
                     static_cast<unsigned>(flags), first_err ? first_err : "unknown");
        SDL_ClearError();
        const Uint32 without_haptic = flags & ~static_cast<Uint32>(SDL_INIT_HAPTIC);
        if (without_haptic == 0 || (rc = real_init(without_haptic)) == 0) {
            return 0;
        }
    }

    if ((flags & ~kOptionalInputFlags) == 0) {
        const char* err = SDL_GetError();
        std::fprintf(stderr,
                     "[omnidesk-sdl] Optional SDL controller/haptic subsystems unavailable (%s); continuing with keyboard/mouse input\n",
                     err ? err : "unknown");
        SDL_ClearError();
        return 0;
    }

    return rc;
}

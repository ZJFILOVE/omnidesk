#include <dlfcn.h>
#include <iostream>
#include <string>
#include <omnidesk/plugin_abi.h>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "Usage: test_abi <so_path> <expected_id>\n";
        return 2;
    }
    void* handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        std::cerr << "dlopen failed: " << dlerror() << "\n";
        return 1;
    }
    auto info_fn = reinterpret_cast<OmniDeskAbiInfoFn>(dlsym(handle, "omnidesk_plugin_abi_info"));
    auto create_fn = reinterpret_cast<OmniDeskPluginCreateFn>(dlsym(handle, "omnidesk_plugin_create"));
    auto destroy_fn = reinterpret_cast<OmniDeskPluginDestroyFn>(dlsym(handle, "omnidesk_plugin_destroy"));
    if (!info_fn || !create_fn || !destroy_fn) {
        std::cerr << "Missing mandatory ABI entrypoints\n";
        dlclose(handle);
        return 1;
    }
    const OmniDeskPluginInfo* info = info_fn();
    if (!info || info->abi_version != OMNIDESK_PLUGIN_ABI_VERSION ||
        std::string(info->id) != argv[2]) {
        std::cerr << "ABI info mismatch\n";
        dlclose(handle);
        return 1;
    }
    void* instance = create_fn();
    if (!instance) {
        std::cerr << "omnidesk_plugin_create returned nullptr\n";
        dlclose(handle);
        return 1;
    }
    destroy_fn(instance);
    dlclose(handle);
    return 0;
}

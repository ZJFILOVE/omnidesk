#include <dlfcn.h>
#include <iostream>
#include <string>
#include <omnidesk/plugin_abi.h>

int main(int argc, char** argv) {
    if (argc < 3) return 2;
    void* h = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!h) {
        std::cerr << dlerror() << "\n";
        return 1;
    }
    auto info_fn = reinterpret_cast<OmniDeskAbiInfoFn>(dlsym(h, "omnidesk_plugin_abi_info"));
    auto create_fn = reinterpret_cast<OmniDeskPluginCreateFn>(dlsym(h, "omnidesk_plugin_create"));
    auto destroy_fn = reinterpret_cast<OmniDeskPluginDestroyFn>(dlsym(h, "omnidesk_plugin_destroy"));
    if (!info_fn || !create_fn || !destroy_fn) {
        dlclose(h);
        return 1;
    }
    const OmniDeskPluginInfo* info = info_fn();
    if (!info || info->abi_version != OMNIDESK_PLUGIN_ABI_VERSION || std::string(info->id) != argv[2]) {
        dlclose(h);
        return 1;
    }
    void* inst = create_fn();
    if (!inst) {
        dlclose(h);
        return 1;
    }
    destroy_fn(inst);
    dlclose(h);
    return 0;
}

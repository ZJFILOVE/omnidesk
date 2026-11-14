#include "omnidesk/plugin_abi.h"

int main(void) {
    struct OmniDeskPluginInfo info = {0};
    info.abi_version = OMNIDESK_PLUGIN_ABI_VERSION;
    info.struct_size = sizeof(info);
    return info.abi_version == 1u && info.struct_size >= sizeof(uint32_t) * 2 ? 0 : 1;
}

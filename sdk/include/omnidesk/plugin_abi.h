#pragma once

#include <stdint.h>
#include "omnidesk/export.h"

#ifdef __cplusplus
namespace omnidesk {
class IPlugin;
}
extern "C" {
#endif

/**
 * Bump only when breaking the binary layout or mandatory entrypoints
 * between the OmniDesk host (libomnidesk_sdk.so) and standalone .so plugins.
 */
#define OMNIDESK_PLUGIN_ABI_VERSION 1u
#define OMNIDESK_SDK_VERSION_STRING "0.1.0"
#ifndef OMNIDESK_PLUGIN_INTERFACE_REVISION
#define OMNIDESK_PLUGIN_INTERFACE_REVISION 2u
#endif

/**
 * Pure C POD handshake struct inspected immediately after dlopen()
 * before instantiating any C++ plugin class.
 */
struct OmniDeskPluginInfo {
    uint32_t abi_version;
    uint32_t struct_size;
    const char* id;
    const char* name;
    const char* version;
    const char* kind;
    const char* domain;
    const char* sdk_version;
    const char* sdk_abi_signature;
};

typedef const struct OmniDeskPluginInfo* (*OmniDeskAbiInfoFn)(void);
typedef void* (*OmniDeskPluginCreateFn)(void);
typedef void (*OmniDeskPluginDestroyFn)(void*);

OMNIDESK_API const char* omnidesk_sdk_abi_signature(void);

#ifdef __cplusplus
} // extern "C"

#include "omnidesk/abi_fingerprint.hpp"

/**
 * Declare the canonical C ABI entrypoints inside a standalone plugin .so.
 * Usage:
 *   OMNIDESK_DECLARE_PLUGIN(MyPlugin, "plugin-my-name", "My Plugin", "0.1.0", "provider", "core")
 */
#define OMNIDESK_DECLARE_PLUGIN(PluginClass, PluginId, PluginName, PluginVersion, PluginKind, PluginDomain) \
    extern "C" OMNIDESK_PLUGIN_EXPORT const OmniDeskPluginInfo* omnidesk_plugin_abi_info(void) { \
        static constexpr auto kAbiSig = ::omnidesk::detail::computeHeaderSdkAbiSignature(); \
        static constexpr OmniDeskPluginInfo kInfo = { \
            OMNIDESK_PLUGIN_ABI_VERSION, \
            static_cast<uint32_t>(sizeof(OmniDeskPluginInfo)), \
            PluginId, \
            PluginName, \
            PluginVersion, \
            PluginKind, \
            PluginDomain, \
            OMNIDESK_SDK_VERSION_STRING, \
            kAbiSig.c_str(), \
        }; \
        return &kInfo; \
    } \
    extern "C" OMNIDESK_PLUGIN_EXPORT void* omnidesk_plugin_create(void) { \
        return static_cast<void*>(static_cast<::omnidesk::IPlugin*>(new PluginClass())); \
    } \
    extern "C" OMNIDESK_PLUGIN_EXPORT void omnidesk_plugin_destroy(void* instance) { \
        delete static_cast<::omnidesk::IPlugin*>(instance); \
    }
#endif

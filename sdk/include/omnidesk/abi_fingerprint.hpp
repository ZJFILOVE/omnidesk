#pragma once

#ifdef __cplusplus

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>
#include <omnidesk/abi_build_signature.h>
#include <omnidesk/async.hpp>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/domain_registry.hpp>
#include <omnidesk/job_center.hpp>
#include <omnidesk/plugin.hpp>
#include <omnidesk/plugin_abi.h>
#include <omnidesk/types.hpp>
#include <omnidesk/worker_supervisor.hpp>

namespace omnidesk::detail {

static constexpr uint64_t abiMix64(uint64_t hash, uint64_t value) noexcept {
    for (int byte = 0; byte < 8; ++byte) {
        hash ^= value & 0xFFu;
        hash *= 1099511628211ULL;
        value >>= 8;
    }
    return hash;
}

static constexpr uint64_t abiMixStr(uint64_t hash, const char* value) noexcept {
    for (; *value; ++value) {
        hash ^= static_cast<unsigned char>(*value);
        hash *= 1099511628211ULL;
    }
    return abiMix64(hash, 0xFFu);
}

template <typename... Types>
static constexpr uint64_t abiMixLayouts(uint64_t hash) noexcept {
    ((hash = abiMix64(abiMix64(hash, sizeof(Types)), alignof(Types))), ...);
    return hash;
}

static constexpr uint64_t headerSdkAbiHash() noexcept {
    uint64_t hash = abiMixStr(14695981039346656037ULL, OMNIDESK_SDK_HEADER_SIGNATURE);
    hash = abiMixStr(hash, OMNIDESK_SDK_VERSION_STRING);
    hash = abiMix64(hash, OMNIDESK_PLUGIN_ABI_VERSION);
    hash = abiMix64(hash, OMNIDESK_CAPABILITY_INTERFACE_REVISION);
    hash = abiMix64(hash, OMNIDESK_DOMAIN_INTERFACE_REVISION);
    hash = abiMix64(hash, OMNIDESK_WORKER_INTERFACE_REVISION);
    hash = abiMix64(hash, OMNIDESK_PLUGIN_INTERFACE_REVISION);
    hash = abiMix64(hash, sizeof(void*));
    hash = abiMix64(hash, __cplusplus);
#ifdef __GXX_ABI_VERSION
    hash = abiMix64(hash, __GXX_ABI_VERSION);
#endif
#ifdef __BYTE_ORDER__
    hash = abiMix64(hash, __BYTE_ORDER__);
#endif
#ifdef _GLIBCXX_USE_CXX11_ABI
    hash = abiMix64(hash, _GLIBCXX_USE_CXX11_ABI);
#endif
#ifdef __GLIBCXX__
    hash = abiMix64(hash, __GLIBCXX__);
#endif
#ifdef _GLIBCXX_DEBUG
    hash = abiMixStr(hash, "glibcxx-debug");
#endif
#ifdef _LIBCPP_VERSION
    hash = abiMix64(hash, _LIBCPP_VERSION);
#endif
#ifdef _LIBCPP_ABI_VERSION
    hash = abiMix64(hash, _LIBCPP_ABI_VERSION);
#endif
    hash = abiMix64(hash, NLOHMANN_JSON_VERSION_MAJOR);
    hash = abiMix64(hash, NLOHMANN_JSON_VERSION_MINOR);
    hash = abiMix64(hash, NLOHMANN_JSON_VERSION_PATCH);
    hash = abiMix64(hash, JSON_DIAGNOSTICS);
#ifdef JSON_DIAGNOSTIC_POSITIONS
    hash = abiMix64(hash, JSON_DIAGNOSTIC_POSITIONS);
#endif
    return abiMixLayouts<
        std::string, std::vector<std::string>, nlohmann::json, std::function<void()>,
        OmniDeskPluginInfo, IPlugin, IMediaSource, ITextSource, IBookSource, PluginContext,
        CapabilityRegistry, DomainRegistry, EntityBus, ConfigStore, SecretVault, JobCenter,
        WorkerSupervisor, CancelToken, ConfigField, WorkspaceTabSpec, DockWidgetSpec,
        MediaItem, LyricLine, ChapterSegment, TextFeedItem, TextParagraph, CommentNode,
        TextArticle, BookItem, BookPageBlock, BookSpread, VocabCard, CapabilityDescriptor,
        CapabilityResult, StreamChunk, JobTarget, JobSpec, JobInfo, JobHandle,
        WorkerSpec, WorkerStatus, WorkerFaultEvent>(hash);
}

struct HeaderAbiSignature {
    char value[sizeof("abi-v" OMNIDESK_SDK_VERSION_STRING "-") + 16]{};
    constexpr const char* c_str() const noexcept { return value; }
};

static constexpr HeaderAbiSignature computeHeaderSdkAbiSignature() noexcept {
    HeaderAbiSignature signature{};
    constexpr char prefix[] = "abi-v" OMNIDESK_SDK_VERSION_STRING "-";
    constexpr char digits[] = "0123456789abcdef";
    constexpr uint64_t hash = headerSdkAbiHash();
    for (size_t index = 0; index < sizeof(prefix) - 1; ++index) {
        signature.value[index] = prefix[index];
    }
    for (size_t index = 0; index < 16; ++index) {
        signature.value[sizeof(prefix) - 1 + index] = digits[(hash >> ((15 - index) * 4)) & 0xF];
    }
    return signature;
}

}

#endif

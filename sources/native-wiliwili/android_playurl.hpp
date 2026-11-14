#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <functional>
#include "bilibili/result/video_detail_result.h"

// No Android runtime or protobuf/gRPC library is required. The caller runs this
// on its existing network worker and retains the web result on any failure.
namespace wiliwili::android {
bool parsePlayReply(std::string_view bytes, bilibili::VideoUrlResult& result);
bool getPlayUrl(const std::string& idKey, const std::string& id, uint64_t cid,
                int quality, int codec, const std::string& sessionPath,
                const std::string& proxy, bilibili::VideoUrlResult& result,
                std::function<bool()> cancelled = {}, const std::string& expectedMid = {});
}

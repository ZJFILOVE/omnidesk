#pragma once

#include <nlohmann/json.hpp>
#include <algorithm>
#include <cctype>
#include <cstdint>
#include <string>

namespace wiliwili::adfilter {

inline std::string trim(std::string value) {
    auto notSpace = [](unsigned char ch) { return !std::isspace(ch); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), notSpace));
    value.erase(std::find_if(value.rbegin(), value.rend(), notSpace).base(), value.end());
    return value;
}

inline std::string trimLower(std::string value) {
    value = trim(std::move(value));
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return value;
}

inline bool isNonEmptyValue(const nlohmann::json& val) {
    if (val.is_null()) return false;
    if (val.is_boolean()) return val.get<bool>();
    if (val.is_number_integer()) return val.get<int64_t>() != 0;
    if (val.is_number_unsigned()) return val.get<uint64_t>() != 0;
    if (val.is_number_float()) return val.get<double>() != 0.0;
    if (val.is_string()) return !trim(val.get<std::string>()).empty();
    if (val.is_array() || val.is_object()) return !val.empty();
    return false;
}

inline bool hasNonEmptyField(const nlohmann::json& obj, const char* key) {
    if (!obj.is_object()) return false;
    auto it = obj.find(key);
    return it != obj.end() && isNonEmptyValue(*it);
}

inline bool isTruthyFlag(const nlohmann::json& obj, const char* key) {
    if (!obj.is_object()) return false;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return false;
    if (it->is_boolean()) return it->get<bool>();
    if (it->is_number_integer()) return it->get<int64_t>() != 0;
    if (it->is_number_unsigned()) return it->get<uint64_t>() != 0;
    if (it->is_string()) {
        std::string lower = trimLower(it->get<std::string>());
        return lower == "1" || lower == "true";
    }
    return false;
}

inline int64_t readInt64(const nlohmann::json& obj, const char* key) {
    if (!obj.is_object()) return 0;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return 0;
    if (it->is_number_integer()) return it->get<int64_t>();
    if (it->is_number_unsigned()) return static_cast<int64_t>(it->get<uint64_t>());
    if (it->is_string()) {
        try {
            return std::stoll(trim(it->get<std::string>()));
        } catch (...) {
            return 0;
        }
    }
    return 0;
}

inline bool isAdLabel(const std::string& raw) {
    std::string trimmed = trim(raw);
    if (trimmed.empty()) return false;
    if (trimmed == "广告" || trimmed == "商业推广" || trimmed == "创作推广" ||
        trimmed == "推广" || trimmed == "商业广告" || trimmed == "广告推广") {
        return true;
    }
    return trimLower(trimmed) == "ad";
}

inline bool isAdUri(const std::string& raw) {
    std::string lower = trimLower(raw);
    if (lower.empty()) return false;
    return lower.rfind("bilibili://cm", 0) == 0 ||
           lower.rfind("bilibili://ad", 0) == 0 ||
           lower.find("cm.bilibili.com") != std::string::npos ||
           lower.find("ad_cb=") != std::string::npos ||
           lower.find("creative_id=") != std::string::npos;
}

inline bool isAdBusinessMark(const nlohmann::json& mark) {
    if (!mark.is_object()) return false;
    return hasNonEmptyField(mark, "text") || hasNonEmptyField(mark, "img_url");
}

inline bool isAdBusinessInfo(const nlohmann::json& obj, const char* key = "business_info") {
    if (!obj.is_object()) return false;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return false;
    const auto& val = *it;
    if (!val.is_object()) return isNonEmptyValue(val);
    if (val.empty()) return false;
    if (isTruthyFlag(val, "is_ad") ||
        hasNonEmptyField(val, "adver_name") ||
        hasNonEmptyField(val, "url") ||
        hasNonEmptyField(val, "ad_cb") ||
        hasNonEmptyField(val, "archive") ||
        readInt64(val, "creative_id") > 0) {
        return true;
    }
    auto markIt = val.find("business_mark");
    if (markIt != val.end() && isAdBusinessMark(*markIt)) return true;
    auto flagIt = val.find("is_ad");
    if (flagIt != val.end() && flagIt->is_boolean() && !flagIt->get<bool>()) return false;
    return true;
}

inline bool isAdCardType(const std::string& raw) {
    std::string lower = trimLower(raw);
    if (lower.empty()) return false;
    return lower == "ad" || lower == "cm" || lower == "cm_v1" || lower == "cm_v2" ||
           lower == "ad_web_s" || lower == "ad_av" || lower == "ad_player" ||
           lower == "banner_v1" || lower.rfind("ad_", 0) == 0 || lower.rfind("cm_", 0) == 0;
}

inline bool isNativeAdJson(const nlohmann::json& item) {
    if (!item.is_object()) return false;
    if (isTruthyFlag(item, "is_ad") ||
        isTruthyFlag(item, "is_commercial") ||
        isAdBusinessInfo(item) ||
        hasNonEmptyField(item, "ad_info") ||
        hasNonEmptyField(item, "cm_info") ||
        hasNonEmptyField(item, "ad_cb") ||
        hasNonEmptyField(item, "adver_name") ||
        readInt64(item, "creative_id") > 0) {
        return true;
    }
    if (auto it = item.find("business_mark"); it != item.end() && isAdBusinessMark(*it)) {
        return true;
    }
    for (const char* key : {"card_type", "card_style"}) {
        if (auto it = item.find(key); it != item.end() && it->is_string()) {
            if (isAdCardType(it->get<std::string>())) return true;
        }
    }
    if (auto it = item.find("goto"); it != item.end() && it->is_string()) {
        std::string lower = trimLower(it->get<std::string>());
        if (lower == "ad" || lower == "cm" || lower == "game" || lower == "banner" ||
            lower == "special" || lower.rfind("ad_", 0) == 0 || lower.rfind("cm_", 0) == 0) {
            return true;
        }
    }
    if (auto it = item.find("rcmd_reason"); it != item.end() && !it->is_null()) {
        if (it->is_string() && isAdLabel(it->get<std::string>())) return true;
        if (it->is_object()) {
            if (auto c = it->find("content"); c != it->end() && c->is_string() && isAdLabel(c->get<std::string>())) {
                return true;
            }
        }
    }
    if (auto it = item.find("badge"); it != item.end() && it->is_object()) {
        if (auto t = it->find("text"); t != it->end() && t->is_string() && isAdLabel(t->get<std::string>())) {
            return true;
        }
    }
    if (auto it = item.find("badges"); it != item.end() && it->is_array()) {
        for (const auto& badge : *it) {
            if (badge.is_object()) {
                if (auto t = badge.find("text"); t != badge.end() && t->is_string() && isAdLabel(t->get<std::string>())) {
                    return true;
                }
            }
        }
    }
    for (const char* key : {"uri", "url"}) {
        if (auto it = item.find(key); it != item.end() && it->is_string()) {
            if (isAdUri(it->get<std::string>())) return true;
        }
    }
    return false;
}

inline bool isNativeAdDynamicJson(const nlohmann::json& item) {
    if (!item.is_object()) return false;
    if (isNativeAdJson(item)) return true;
    if (auto it = item.find("type"); it != item.end() && it->is_string()) {
        const std::string t = it->get<std::string>();
        if (t == "DYNAMIC_TYPE_AD" || t.rfind("DYNAMIC_TYPE_AD", 0) == 0) return true;
    }
    if (auto it = item.find("orig"); it != item.end() && it->is_object()) {
        if (isNativeAdDynamicJson(*it)) return true;
    }
    if (auto it = item.find("modules"); it != item.end()) {
        if (it->is_array()) {
            for (const auto& mod : *it) {
                if (!mod.is_object()) continue;
                if (auto mt = mod.find("module_type"); mt != mod.end() && mt->is_string() && *mt == "MODULE_TYPE_AD") {
                    return true;
                }
                if (auto md = mod.find("module_dynamic"); md != mod.end() && md->is_object()) {
                    if (auto dt = md->find("type"); dt != md->end() && dt->is_string() && *dt == "MDL_DYN_TYPE_AD") {
                        return true;
                    }
                }
                if (isNativeAdJson(mod)) return true;
            }
        } else if (it->is_object()) {
            if (it->contains("module_ad")) return true;
            if (auto md = it->find("module_dynamic"); md != it->end() && md->is_object()) {
                if (auto dt = md->find("type"); dt != md->end() && dt->is_string() && *dt == "MDL_DYN_TYPE_AD") {
                    return true;
                }
            }
            for (auto mIt = it->begin(); mIt != it->end(); ++mIt) {
                if (mIt->is_object() && isNativeAdJson(*mIt)) return true;
            }
        }
    }
    return false;
}

}  // namespace wiliwili::adfilter

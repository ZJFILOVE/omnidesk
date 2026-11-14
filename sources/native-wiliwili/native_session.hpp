#pragma once
#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <nlohmann/json.hpp>

namespace wiliwili::auth {
using Cancel = std::function<bool()>;
using Json = nlohmann::json;
enum class State { Missing, Valid, Expired, NetworkError, Rejected, Cancelled };
struct Account {
    State state = State::Missing;
    std::string mid, name;
    bool vip = false;
    int httpStatus = 0, transportError = 0;
};
enum class QrState { Preparing, Ready, Scanned, Success, Expired, Error, Cancelled };
struct Challenge {
    bool app = true;
    std::string url, key;
    int64_t deadline = 0;
};
struct Poll {
    QrState state = QrState::Error;
    Json session;
    std::map<std::string, std::string> cookies;
    std::string webRefresh;
};
// The private data returned by these functions must never be logged/diagnosed.
bool privatePath(const std::string& path);
Json readPrivate(const std::string& path);
bool writePrivate(const std::string& path, const Json& data);
bool removePrivate(const std::string& path);
bool expired(const Json& data);
Account appAccount(const Json& session, const std::string& proxy, Cancel cancel = {});
Account webAccount(const std::map<std::string, std::string>& cookies, const std::string& proxy, Cancel cancel = {});
bool refresh(Json& session, const std::string& proxy, Cancel cancel = {});
Json usableSession(const std::string& path, const std::string& expectedMid, const std::string& proxy, Cancel cancel = {});
Challenge prepare(bool app, const std::string& proxy, Cancel cancel = {});
Poll poll(const Challenge& qr, const std::string& proxy, Cancel cancel = {});
QrState pollState(bool app, const Json& reply, int64_t deadline);
std::map<std::string, std::string> sessionCookies(const Json& session);
std::string token(const Json& session);
uint64_t generation();
void changed();
}

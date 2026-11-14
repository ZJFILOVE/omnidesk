#include "utils/native_session.hpp"
#include <cassert>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char** argv) {
    using namespace wiliwili::auth;
    if (argc == 3 && std::string(argv[1]) == "--check") {
        const auto session = readPrivate(argv[2]);
        const auto app = appAccount(session, "");
        const auto web = webAccount(sessionCookies(session), "");
        std::cout << Json({{"evidence", "real_service"}, {"app_valid", app.state == State::Valid},
            {"web_cookie_valid", web.state == State::Valid}, {"same_account", web.mid == app.mid && !web.mid.empty()},
            {"vip", app.vip}, {"app_state", int(app.state)}, {"web_state", int(web.state)}, {"http_status", app.httpStatus}, {"transport_error", app.transportError}, {"expired_locally", expired(session)}}).dump() << std::endl;
        return app.state == State::Valid ? 0 : 1;
    }
    char dir[] = "/tmp/wiliwili-auth-unit.XXXXXX";
    assert(mkdtemp(dir));
    const std::string path = std::string(dir) + "/session.json";
    const Json fake = {{"access_token", std::string(32, 'x')}, {"created", std::time(nullptr)}, {"expires_in", 3600}};
    assert(writePrivate(path, fake)); assert(readPrivate(path) == fake); assert(!expired(fake));
    struct stat st{}; assert(stat(path.c_str(), &st) == 0); assert((st.st_mode & 0777) == 0600);
    chmod(path.c_str(), 0644); assert(readPrivate(path).is_null()); chmod(path.c_str(), 0600);
    const std::string link = std::string(dir) + "/link"; assert(symlink(path.c_str(), link.c_str()) == 0);
    assert(readPrivate(link).is_null());
    std::filesystem::create_directory(std::string(dir) + "/.git");
    std::ofstream(std::string(dir) + "/.git/HEAD") << "ref: refs/heads/test";
    assert(!writePrivate(path, fake)); std::filesystem::remove_all(std::string(dir) + "/.git");
    std::ofstream(path) << "{invalid"; assert(readPrivate(path).is_null());
    assert(expired(Json{{"access_token", 123}})); assert(expired(Json{{"access_token", std::string(32, 'x')}, {"created", "oops"}}));
    auto past = fake; past["created"] = 1; assert(expired(past));
    const int64_t future = std::time(nullptr) + 180;
    for (const auto app : {false, true}) {
        for (const auto item : {std::pair{0, QrState::Success}, {86038, QrState::Expired}, {86090, QrState::Scanned},
                               {86039, QrState::Ready}, {86101, QrState::Ready}, {-400, QrState::Error}}) {
            const Json reply = app ? Json{{"code", item.first}} : Json{{"code", 0}, {"data", {{"code", item.first}}}};
            assert(pollState(app, reply, future) == item.second);
        }
    }
    assert(pollState(true, {{"code", "bad"}}, future) == QrState::Error);
    assert(pollState(true, {{"code", 0}}, 0) == QrState::Expired);
    assert(prepare(true, "", []{return true;}).url.empty());
    assert(poll({}, "", []{return true;}).state == QrState::Cancelled);
    const auto before = generation(); changed(); assert(generation() != before);
    assert(removePrivate(path)); std::filesystem::remove_all(dir);
    std::cout << "{\"evidence\":\"synthetic_unit_cases\",\"private_storage\":true,\"qr_states\":true,\"cancellation\":true,\"passed\":true}" << std::endl;
}

#include "utils/session_ui.hpp"
#include "utils/native_session.hpp"
#include "utils/config_helper.hpp"
#include "utils/ai_helper.hpp"
#include "utils/event_helper.hpp"
#include "bilibili.h"
#include "bilibili/util/http.hpp"
#include "view/qr_image.hpp"
#include <borealis.hpp>
#include <ctime>

namespace SessionUi {
namespace {
using namespace wiliwili::auth;
Json diagnostic = {{"open", false}};
std::string path() {
    if (const char* p = std::getenv("WILI_ANDROID_SESSION")) return p;
    return ProgramConfig::instance().getConfigDir() + "/android-session.json";
}
std::string status(const Account& a) {
    switch (a.state) {
        case State::Valid: return "已登录 · " + a.name;
        case State::Missing: return "未登录";
        case State::Expired: return "已失效，请重新扫码";
        case State::NetworkError: return "网络检查失败，可重试";
        case State::Cancelled: return "已取消";
        default: return "服务器未接受此会话，请重新扫码";
    }
}
void webCookies(const std::map<std::string, std::string>& cookies, const std::string& refreshToken) {
    ProgramConfig::instance().setCookie(cookies);
    ProgramConfig::instance().setRefreshToken(refreshToken);
    bilibili::HTTP::COOKIES = {false};
    for (const auto& item : cookies) bilibili::HTTP::COOKIES.emplace_back({item.first, item.second});
    bilibili::HTTP::HEADERS["cookie"] = bilibili::HTTP::getEncodedCookie(bilibili::HTTP::COOKIES);
}
class SessionView : public brls::Box {
    struct Life { SessionView* view; std::atomic<bool> cancelled{false}; explicit Life(SessionView* v): view(v) {} };
    std::shared_ptr<Life> life = std::make_shared<Life>(this);
    std::shared_ptr<std::atomic<bool>> operation = std::make_shared<std::atomic<bool>>(false);
    brls::Label *summary, *hint, *clock;
    QRImage* qr;
    Challenge challenge;
    std::function<void()> onChanged;
    bool committed = false;
    size_t timer = 0;
    Cancel cancellation() {
        auto l = life; auto op = operation; const auto e = generation();
        return [l, op, e]{ return l->cancelled.load() || op->load() || e != generation(); };
    }
    void cancelOperation() {
        *operation = true; operation = std::make_shared<std::atomic<bool>>(false);
        if (timer) { brls::cancelDelay(timer); timer = 0; }
        challenge = {}; qr->setVisibility(brls::Visibility::GONE); clock->setText("");
    }
    void didChange() {
        changed(); committed = true;
        AiHelper::instance().invalidate(nullptr);
        APP_E->fire("NATIVE_ACCOUNT_CHANGED", nullptr);
        if (onChanged) onChanged();
    }
    brls::Button* button(const std::string& title, std::function<void()> action) {
        auto* b = new brls::Button(); b->setText(title); b->setHeight(42); b->setMarginTop(6);
        b->registerClickAction([action](brls::View*){ action(); return true; }); addView(b); return b;
    }
    void inspect(bool doRefresh) {
        cancelOperation(); hint->setText(doRefresh ? "正在刷新高清授权…" : "正在校验服务器会话…");
        diagnostic["stage"] = "checking";
        auto l = life; auto op = operation; auto cancel = cancellation();
        const auto p = path(), proxy = ProgramConfig::instance().getProxy(); const auto cookies = ProgramConfig::instance().getCookie();
        brls::async([l, op, cancel, p, proxy, cookies, doRefresh] {
            auto session = readPrivate(p);
            bool refreshed = false;
            if (doRefresh || (!token(session).empty() && expired(session))) refreshed = refresh(session, proxy, cancel);
            const auto web = webAccount(cookies, proxy, cancel);
            const auto app = appAccount(session, proxy, cancel);
            brls::sync([l, op, cancel, p, session, refreshed, doRefresh, web, app]{
                if (cancel() || !l->view) return;
                auto* v = l->view;
                if (refreshed && !writePrivate(p, session)) { v->hint->setText("无法安全保存凭据。请检查私有配置目录权限。"); return; }
                if (refreshed) v->didChange();
                const bool same = web.state == State::Valid && app.state == State::Valid && web.mid == app.mid;
                std::string note = same ? "两个会话属于同一账号" : (web.state == State::Valid && app.state == State::Valid ?
                    "账号不同：高清授权暂停使用，请用相同账号重新扫码" : "B站登录用于收藏、历史和字幕；高清授权用于 App 取流");
                v->summary->setText("B站登录：" + status(web) + "\n高清播放授权：" + status(app) + "\n" + note);
                v->hint->setText(doRefresh && !refreshed ? "此会话无法刷新，请点“高清播放扫码 / 重新授权”。现有普通播放仍可用。" :
                    (app.state == State::Valid ? "已复用有效授权，无需再次扫码。高清流以服务器实际返回为准。" : "可选择下方原生扫码入口。"));
                diagnostic = {{"open", true}, {"stage", "checked"}, {"web_valid", web.state == State::Valid},
                    {"app_valid", app.state == State::Valid}, {"same_account", same}, {"refreshed", refreshed}};
            });
        });
    }
    void schedulePoll() {
        auto l = life; auto op = operation;
        timer = brls::delay(1000, [l, op] {
            if (l->cancelled || *op || !l->view) return;
            l->view->timer = 0;
            auto* v = l->view;
            const auto remaining = std::max<int64_t>(0, v->challenge.deadline - std::time(nullptr));
            v->clock->setText("二维码剩余 " + std::to_string(remaining) + " 秒 · 可取消或重新生成");
            v->checkQr();
        });
    }
    void checkQr() {
        auto l = life; auto cancel = cancellation(); auto q = challenge;
        const auto proxy = ProgramConfig::instance().getProxy(); const auto cookies = ProgramConfig::instance().getCookie();
        brls::async([l, cancel, q, proxy, cookies] {
            auto result = poll(q, proxy, cancel);
            Account app, web, current;
            if (result.state == QrState::Success) {
                if (q.app) {
                    app = appAccount(result.session, proxy, cancel);
                    current = webAccount(cookies, proxy, cancel);
                    web = webAccount(result.cookies, proxy, cancel);
                } else web = webAccount(result.cookies, proxy, cancel);
            }
            brls::sync([l, cancel, q, result, app, web, current] {
                if (cancel() || !l->view) return;
                auto* v = l->view;
                switch (result.state) {
                    case QrState::Ready: v->hint->setText("二维码已准备好，请用手机 B站扫码"); diagnostic["stage"] = "ready"; v->schedulePoll(); break;
                    case QrState::Scanned: v->hint->setText("已扫码，请在手机上确认授权"); diagnostic["stage"] = "scanned"; v->schedulePoll(); break;
                    case QrState::Expired: v->qr->setVisibility(brls::Visibility::GONE); v->clock->setText(""); v->hint->setText("二维码已过期，请点对应扫码按钮刷新"); diagnostic["stage"] = "expired"; break;
                    case QrState::Success: {
                        if ((q.app && app.state != State::Valid) || (!q.app && web.state != State::Valid)) {
                            v->hint->setText("扫码已确认，但服务器会话检查失败。请重试。"); diagnostic["stage"] = "error"; return;
                        }
                        if (q.app && current.state == State::Valid && current.mid != app.mid) {
                            v->hint->setText("扫码账号与当前 B站登录不同，未保存此授权。请用相同账号重试，或先退出全部登录。");
                            v->qr->setVisibility(brls::Visibility::GONE); diagnostic["stage"] = "mismatch"; return;
                        }
                        if (q.app && !writePrivate(path(), result.session)) {
                            v->hint->setText("不能安全保存授权：凭据必须在仓库外，文件权限为 0600。"); diagnostic["stage"] = "error"; return;
                        }
                        if (web.state == State::Valid && (!q.app || web.mid == app.mid)) webCookies(result.cookies, result.webRefresh);
                        v->cancelOperation(); v->didChange(); v->summary->setText("授权成功 · 已校验服务器账号");
                        v->hint->setText(q.app && web.state == State::Valid ? "B站登录与高清播放授权已统一到同一账号。" : "登录成功，可关闭此窗口继续使用。");
                        diagnostic = {{"open", true}, {"stage", "success"}, {"app_valid", q.app}, {"web_valid", web.state == State::Valid},
                            {"same_account", q.app && web.state == State::Valid && web.mid == app.mid}};
                        brls::Application::notify("授权成功，已安全保存会话"); break;
                    }
                    default: v->hint->setText("网络或授权请求失败。可点对应扫码按钮重试，或取消。"); diagnostic["stage"] = "error"; break;
                }
            });
        });
    }
    void startQr(bool app) {
        cancelOperation(); hint->setText("正在生成二维码，请稍候…"); diagnostic["stage"] = "preparing";
        auto l = life; auto cancel = cancellation(); const auto proxy = ProgramConfig::instance().getProxy();
        brls::async([l, cancel, app, proxy]{
            const auto q = prepare(app, proxy, cancel);
            brls::sync([l, cancel, q]{
                if (cancel() || !l->view) return;
                auto* v = l->view;
                if (q.url.empty()) { v->hint->setText("二维码生成失败，请重试。取消不影响已有登录。"); diagnostic["stage"] = "error"; return; }
                v->challenge = q; v->qr->setImageFromQRContent(q.url); v->qr->setVisibility(brls::Visibility::VISIBLE);
                v->hint->setText("二维码已准备好，请用手机 B站扫码并确认");
                v->clock->setText("二维码剩余 " + std::to_string(q.deadline - std::time(nullptr)) + " 秒");
                diagnostic["stage"] = "ready"; diagnostic["qr_ready"] = true;
                brls::Application::notify("二维码已准备好，请扫码确认"); v->schedulePoll();
            });
        });
    }
public:
    explicit SessionView(std::function<void()> callback): brls::Box(brls::Axis::COLUMN), onChanged(std::move(callback)) {
        setPadding(16); setWidth(540);
        summary = new brls::Label(); summary->setText("登录与高清播放授权"); summary->setFontSize(18); addView(summary);
        qr = new QRImage(); qr->setWidth(190); qr->setHeight(190); qr->setQRBorder(4); qr->setAlignSelf(brls::AlignSelf::CENTER);
        qr->setVisibility(brls::Visibility::GONE); addView(qr);
        clock = new brls::Label(); clock->setFontSize(14); addView(clock);
        hint = new brls::Label(); hint->setFontSize(15); hint->setMarginTop(6); addView(hint);
        button("高清播放扫码 / 重新授权", [this]{ startQr(true); });
        button("B站登录扫码", [this]{ startQr(false); });
        button("检查会话 / 复用授权", [this]{ inspect(false); });
        button("刷新高清授权", [this]{ inspect(true); });
        button("退出高清授权", [this]{ cancelOperation(); if (removePrivate(path())) { didChange(); inspect(false); } else hint->setText("删除授权失败，请检查私有目录权限。"); });
        button("退出全部登录", [this]{ cancelOperation(); if (!removePrivate(path())) { hint->setText("删除授权失败"); return; } webCookies({}, ""); didChange(); inspect(false); });
        inspect(false);
    }
    ~SessionView() override {
        life->cancelled = true; life->view = nullptr; *operation = true; if (timer) brls::cancelDelay(timer);
        diagnostic = {{"open", false}, {"stage", committed ? "closed" : "cancelled"}};
    }
};
}
Json diagnostics() { return diagnostic; }
void open(std::function<void()> onChanged) {
    if (diagnostic.value("open", false)) return;
    diagnostic = {{"open", true}, {"stage", "checking"}};
    auto* dialog = new brls::Dialog(new SessionView(std::move(onChanged)));
    dialog->addButton("取消 / 关闭", []{}); dialog->open();
}
}

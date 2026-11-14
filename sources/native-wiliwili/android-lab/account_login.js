// Pass the QR-approved session to the official account API. No fake VIP state.
rpc.exports.login = function (session) {
    return new Promise((resolve, reject) => Java.perform(function () { try {
        const App = Java.use('android.app.ActivityThread').currentApplication();
        const Accounts = Java.use('com.bilibili.lib.accounts.BiliAccounts').get(App);
        const Token = Java.use('com.bilibili.lib.accounts.model.AccessToken').$new();
        Token.mAccessKey.value = session.access_token;
        Token.mRefreshToken.value = session.refresh_token;
        Token.mMid.value = session.mid;
        Token.mExpiresIn.value = session.expires_in;
        Token.mExpires.value = Math.floor(Date.now() / 1000) + session.expires_in;
        const Auth = Java.use('com.bilibili.lib.accounts.model.AuthInfo').$new();
        Auth.accessToken.value = Token;
        Auth.status.value = 0;
        const Cookies = Java.use('com.bilibili.lib.accounts.model.CookieInfo').$new();
        Cookies.cookies.value = Java.use('java.util.ArrayList').$new();
        Cookies.domains.value = Java.array('java.lang.String', []);
        Auth.cookieInfo.value = Cookies;
        Accounts.signedInWithToken(Auth);
        const state = {official_account_api_called: true,
            is_login: Accounts.isLogin(), token_valid: Accounts.isTokenValid(),
            stored_token_matches: String(Accounts.getAccessKey()) === session.access_token};
        try {
            const AccountInfo = Java.use('com.bilibili.lib.accountinfo.BiliAccountInfo').get();
            const info = AccountInfo.requestForAccountInfoByAccessKey(session.access_token);
            const vip = info.getVipInfo();
            state.server_account_accepted = Number(info.getMid()) === Number(session.mid);
            state.vip_type = vip.getVipType();
            state.vip_status = vip.getVipStatus();
            state.effective_vip = vip.isEffectiveVip();
            AccountInfo.saveAccountInfo(info);
        } catch (_) { state.account_info_request_failed = true; }
        send(state); resolve(state);
    } catch (_) { reject(new Error('Official account initialization failed; credentials omitted')); } }));
};

Java.perform(function () {
    const names = ['com.bilibili.lib.accounts.model.AuthInfo',
        'com.bilibili.lib.accounts.model.AccessToken',
        'com.bilibili.lib.accounts.model.CookieInfo',
        'com.bilibili.lib.accountinfo.model.AccountInfo',
        'com.bilibili.lib.accountinfo.model.VipUserInfo'];
    for (const name of names) {
        try {
            const cls = Java.use(name);
            send({class: name,
                fields: cls.class.getDeclaredFields().map(f => String(f)),
                constructors: cls.class.getDeclaredConstructors().map(c => String(c)),
                methods: cls.class.getDeclaredMethods().map(m => String(m)).filter(s =>
                    /Access|[Tt]oken|[Ll]ogin|[Mm]id|[Aa]ccess|[Aa]uth|[Vv]ip|[Ss]tatus/.test(s))});
        } catch (_) { send({class: name, unavailable: true}); }
    }
});

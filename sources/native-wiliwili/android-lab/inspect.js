Java.perform(function () {
    const classes = [
        'com.bapis.bilibili.app.playerunite.v1.PlayerMoss',
        'com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReq',
        'com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReply',
        'com.bapis.bilibili.playershared.VideoVod',
        'com.bilibili.lib.moss.api.MossResponseHandler',
        'com.bilibili.lib.moss.api.MossException',
        'com.bilibili.lib.moss.api.Moss',
    ];
    for (const name of classes) {
        try {
            const cls = Java.use(name);
            send({class: name,
                fields: cls.class.getDeclaredFields().map(f => String(f)),
                constructors: cls.class.getDeclaredConstructors().map(c => String(c)),
                methods: cls.class.getDeclaredMethods().map(m => String(m)).filter(s => /get|set|executePlayView|playView|onNext|onError/.test(s))});
        } catch (e) { send({class: name, unavailable: true}); }
    }
    const Inet = Java.use('java.net.InetAddress');
    for (const host of ['passport.bilibili.com', 'api.bilibili.com']) {
        try { send({host, android_dns: Inet.getAllByName(host).map(a => String(a.getHostAddress()))}); }
        catch (e) { send({host, dns_failed: true}); }
    }
    const Vod = Java.use('com.bapis.bilibili.playershared.VideoVod');
    send({is_need_trial_field_number: Vod.IS_NEED_TRIAL_FIELD_NUMBER.value});
});

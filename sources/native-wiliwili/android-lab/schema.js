Java.perform(function () {
    const Reply = Java.use('com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReply');
    const Vod = Reply.getDefaultInstance().getVodInfo();
    const names = [String(Vod.getClass().getName()),
        'com.bapis.bilibili.playershared.VideoVod',
        'com.bapis.bilibili.playershared.StreamInfo',
        'com.bapis.bilibili.playershared.DashVideo',
        String(Vod.getClass().getDeclaredMethods().find(m =>
            String(m.getName()) === 'getDashAudio' && m.getParameterTypes().length === 1).getReturnType().getName()),
        'com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReply'];
    for (const name of names) {
        const cls = Java.use(name);
        const fields = {};
        for (const f of cls.class.getDeclaredFields()) {
            const key = String(f.getName());
            if (key.endsWith('_FIELD_NUMBER')) fields[key] = f.getInt(null);
        }
        send({schema_class: name, fields});
    }
    send({metadata_classes: Java.enumerateLoadedClassesSync().filter(n => /[Mm]etadata/.test(n) && /bili|moss/.test(n)).slice(0, 40)});
});

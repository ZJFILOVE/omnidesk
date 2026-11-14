// Reproduce BiliRoamingX's request flag and its processing of existing streams.
// Never manufacture URLs or change the account's server-side membership.
Java.perform(function () {
    const Player = Java.use('com.bapis.bilibili.app.playerunite.v1.PlayerMoss');
    const Req = Java.use('com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReq');
    const Reply = Java.use('com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReply');
    const Stream = Java.use('com.bapis.bilibili.playershared.Stream');
    const callbacks = new Set();
    function capture(label, value) {
        const bytes = value.toByteArray();
        const data = new Uint8Array(bytes.length);
        for (let i = 0; i < bytes.length; i++) data[i] = bytes[i] & 255;
        send({private: true, label}, data.buffer);
    }
    function patchRequest(req) {
        const copy = Req.parseFrom.overload('[B').call(Req, req.toByteArray());
        const vod = copy.getVod();
        vod.setIsNeedTrial(true);
        vod.setFnval(vod.getFnval() | 4048);
        vod.setFourk(true);
        capture('hook-request', copy);
        send({hook: 'request', is_need_trial: vod.getIsNeedTrial(), qn: String(vod.getQn()), fnval: vod.getFnval()});
        return copy;
    }
    function patchReply(value) {
        if (!value || !Reply.class.isInstance(value)) return;
        const reply = Java.cast(value, Reply);
        capture('hook-server-reply', reply); // Preserve the server's original response.
        reply.clearQnTrialInfo();
        const list = reply.getVodInfo().getStreamListList();
        let changed = 0;
        for (let i = 0; i < list.size(); i++) {
            const stream = Java.cast(list.get(i), Stream);
            if (!stream.hasDashVideo()) continue;
            const info = stream.getStreamInfo();
            if (info.getNeedVip()) { info.setNeedVip(false); info.setVipFree(true); changed++; }
        }
        send({hook: 'reply', existing_stream_flags_changed: changed, trial_info_present: reply.hasQnTrialInfo()});
    }
    const sync = Player.executePlayViewUnite.overload('com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReq');
    sync.implementation = function (req) {
        const reply = sync.call(this, patchRequest(req));
        patchReply(reply);
        return reply;
    };
    const async = Player.playViewUnite.overload('com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReq', 'com.bilibili.lib.moss.api.MossResponseHandler');
    async.implementation = function (req, handler) {
        const name = String(handler.getClass().getName());
        if (!callbacks.has(name)) {
            const Handler = Java.use(name);
            for (const next of Handler.onNext.overloads) {
                if (next.argumentTypes.length !== 1) continue;
                next.implementation = function (reply) { patchReply(reply); return next.call(this, reply); };
            }
            callbacks.add(name);
        }
        return async.call(this, patchRequest(req), handler);
    };
    send({trial_hook_ready: true, account_status_modified: false});
});

// Minimal request-level reproduction of BiliRoamingX's TrialVipQuality path.
// Raw protobuf responses are sent only to observe.py's private state directory.
function varint(n) {
    const bytes = [];
    do { const digit = n % 128; n = Math.floor(n / 128); bytes.push(digit | (n ? 128 : 0)); } while (n);
    return bytes;
}
function message(fields) {
    let bytes = [];
    for (const [field, value] of fields) {
        if (typeof value === 'number') bytes.push(...varint(field * 8), ...varint(value));
        else {
            const data = typeof value === 'string' ? Array.from(value).map(c => c.charCodeAt(0)) : value;
            bytes.push(...varint(field * 8 + 2), ...varint(data.length), ...data);
        }
    }
    return bytes;
}
function capture(label, value, detail) {
    const bytes = value.toByteArray();
    const data = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) data[i] = bytes[i] & 255;
    send({private: true, label, ...detail}, data.buffer);
}
rpc.exports.probe = function () {
    return new Promise((resolve, reject) => Java.perform(function () { try {
        const Request = Java.use('com.bapis.bilibili.app.playerunite.v1.PlayViewUniteReq');
        const Player = Java.use('com.bapis.bilibili.app.playerunite.v1.PlayerMoss');
        const videos = [
            ['BV1heam6TExz', 117370098027243, 42391570547],
            ['BV1fK4y1t7hj', 883362563, 196018899],
        ];
        const results = [];
        for (const [bvid, aid, cid] of videos) {
            for (const trial of [false, true]) {
                const vod = message([[1, aid], [2, cid], [3, 120], [5, 4048], [7, 2], [8, 1], [9, 2], [11, trial ? 1 : 0]]);
                const bytes = message([[1, vod], [2, 'united.player-video-detail.0.0'], [3, 'main.my-history.0.0'], [5, bvid]]);
                const req = Request.parseFrom.overload('[B').call(Request, Java.array('byte', bytes.map(n => n > 127 ? n - 256 : n)));
                const detail = {bvid, trial, qn: 120, fnval: 4048, source: 'official_android_moss'};
                capture('play-request', req, detail);
                try {
                    const reply = Player.$new().executePlayViewUnite(req);
                    capture('play-reply', reply, detail);
                    const info = reply.getVodInfo();
                    const summary = {...detail, quality: info.getQuality(), timelength: String(info.getTimelength()), streams: info.getStreamListCount()};
                    send(summary);
                    results.push(summary);
                } catch (e) {
                    const summary = {...detail, call_failed: true};
                    send(summary); results.push(summary);
                }
            }
        }
        resolve(results);
    } catch (e) { reject(e); } }));
};

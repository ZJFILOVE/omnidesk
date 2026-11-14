"""The two supported latency/continuity choices shared by the UI and worker."""

SUBTITLE_PROFILES = {
    'fast': {
        'title': '快速字幕 · 优先跟上声音',
        'hint': '停顿约 0.4 秒分句，最长 4 秒一段；原文先显示。翻译请求可能增多，积压时跳过较旧待处理片段。',
        'max_seconds': 4, 'silence_ms': 400, 'audio_queue': 1, 'translation_queue': 1,
    },
    'complete': {
        'title': '完整优先 · 较少翻译请求',
        'hint': '停顿约 0.65 秒分句，最长 8 秒一段；悬浮字幕按原文、译文配对更新，等待相对更久。',
        'max_seconds': 8, 'silence_ms': 650, 'audio_queue': 2, 'translation_queue': 3,
    },
}


def subtitle_profile(config):
    name = config.get('subtitle_profile', 'fast')
    if name not in SUBTITLE_PROFILES:
        raise ValueError('未知的字幕响应模式，请重新选择快速字幕或完整优先。')
    return SUBTITLE_PROFILES[name]

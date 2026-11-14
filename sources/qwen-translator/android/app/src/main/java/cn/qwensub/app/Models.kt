package cn.qwensub.app

import java.net.URI
import java.net.URLDecoder
import java.util.UUID

enum class TaskState { QUEUED, RUNNING, WAITING, READY, FAILED, CANCELLED, SKIPPED }

data class DownloadTask(
    val id: String = UUID.randomUUID().toString(),
    val title: String = "准备下载",
    val url: String,
    val videoState: TaskState = TaskState.QUEUED,
    val subtitleState: TaskState = TaskState.QUEUED,
    val videoProgress: Float = 0f,
    val subtitleProgress: Float = 0f,
    val videoPath: String? = null,
    val originalPath: String? = null,
    val zhPath: String? = null,
    val bilingualPath: String? = null,
    val remoteId: String? = null,
    val videoError: String? = null,
    val subtitleError: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
    // Persist before POST. The NAS deduplicates this ID even if its reply is lost.
    val clientRequestId: String = UUID.randomUUID().toString(),
    val remoteRetryRequested: Boolean = false,
    val nasBaseUrl: String? = null,
    val videoGeneration: Int = 0,
    val subtitleGeneration: Int = 0,
    val submissionTitle: String? = null,
    val localCleanupPending: Boolean = false,
    val remoteCleanupPending: Boolean = false,
    val localFilesDeleted: Boolean = false,
    val remoteFilesDeleted: Boolean = false,
    val cleanupError: String? = null,
    val historyHidden: Boolean = false,
    // Distinguish a completed deletion from a later, explicit download retry on a retained task.
    val cleanupHistoryHandled: Boolean = false,
    // NAS media stays on the server; only explicit offline downloads create local video files.
    val nasMediaId: String? = null,
    val nasStreamPath: String? = null,
    val nasSubtitleId: String? = null,
    val nasOriginalPath: String? = null,
    val nasZhPath: String? = null,
    val nasBilingualPath: String? = null,
) {
    val cleanupPending: Boolean get() = localCleanupPending || remoteCleanupPending
    internal val subtitleCleanupBlocked: Boolean get() = cleanupPending || localFilesDeleted || remoteFilesDeleted
}

data class ConnectionSettings(
    val baseUrl: String = "",
    val token: String = "",
    val downloadProxy: String = "",
)

data class NasJob(val id: String, val state: String, val progress: Float, val message: String, val model: String)

/** Only one concrete YouTube video is accepted; never pass arbitrary downloader arguments. */
object InputValidation {
    fun youtubeUrl(value: String): String {
        val uri = runCatching { URI(value.trim()) }.getOrNull()
            ?: throw IllegalArgumentException("请输入有效的 YouTube 视频链接")
        require(uri.scheme in setOf("http", "https") && uri.userInfo == null && uri.port == -1) {
            "请输入 https:// 开头的 YouTube 视频链接"
        }
        val host = uri.host?.lowercase().orEmpty()
        val id = when (host) {
            "youtu.be" -> uri.path.trim('/').takeIf { !it.contains('/') }
            "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com" -> {
                if (uri.path == "/watch") {
                    uri.rawQuery.orEmpty().split('&').map { it.split('=', limit = 2) }
                        .firstOrNull { it[0] == "v" && it.size == 2 }?.get(1)?.let {
                            runCatching { URLDecoder.decode(it, "UTF-8") }.getOrNull()
                        }
                } else {
                    val parts = uri.path.trim('/').split('/')
                    parts.getOrNull(1)?.takeIf { parts.size == 2 && parts[0] in setOf("shorts", "live", "embed") }
                }
            }
            else -> null
        }
        require(id != null && Regex("[A-Za-z0-9_-]{11}").matches(id)) { "仅支持单条 YouTube 视频、Shorts 或直播回放链接" }
        return "https://www.youtube.com/watch?v=$id"
    }

    fun settings(value: ConnectionSettings): ConnectionSettings {
        val base = value.baseUrl.trim().trimEnd('/')
        if (base.isNotEmpty()) {
            val uri = runCatching { URI(base) }.getOrNull()
            require(uri != null && uri.host != null && uri.userInfo == null && uri.query == null && uri.fragment == null && uri.scheme in setOf("http", "https")) {
                "飞牛地址格式不正确，例如 https://nas.example.ts.net"
            }
            require(uri!!.scheme == "https" || isPrivateHost(uri.host)) { "公网连接必须使用 HTTPS；HTTP 仅用于内网或 Tailscale IP" }
        }
        val proxy = value.downloadProxy.trim()
        if (proxy.isNotEmpty()) {
            val uri = runCatching { URI(proxy) }.getOrNull()
            require(uri != null && uri.scheme in setOf("http", "https", "socks5", "socks5h") && uri.host != null && uri.port in 1..65535 && uri.fragment == null && uri.query == null && uri.path.orEmpty() in setOf("", "/")) {
                "下载代理格式不正确，例如 http://127.0.0.1:7890（手机本地代理）"
            }
        }
        require(!value.token.contains('\n') && !value.token.contains('\r')) { "访问令牌不能包含换行" }
        return value.copy(baseUrl = base, token = value.token.trim(), downloadProxy = proxy)
    }

    private fun isPrivateHost(value: String): Boolean {
        val host = value.lowercase().trim('[', ']')
        if (host == "localhost" || host == "::1" || host.endsWith(".local")) return true
        if (host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80:")) return host.contains(':')
        val parts = host.split('.').map { it.toIntOrNull() ?: return false }
        if (parts.size != 4 || parts.any { it !in 0..255 }) return false
        return parts[0] == 10 || parts[0] == 127 ||
            parts[0] == 192 && parts[1] == 168 ||
            parts[0] == 172 && parts[1] in 16..31 ||
            parts[0] == 100 && parts[1] in 64..127
    }
}

internal fun safeError(error: Throwable): String {
    val message = error.message.orEmpty().lineSequence().lastOrNull { it.contains("ERROR:") }
        ?: error.message.orEmpty().lineSequence().firstOrNull().orEmpty()
    return message.replace(Regex("(?i)bearer\\s+\\S+"), "Bearer [已隐藏]")
        .replace(Regex("https?://\\S+|socks5h?://\\S+"), "[地址已隐藏]")
        .replace(Regex("/data/[^\\s]+"), "[本地文件]")
        .take(300).ifBlank { "操作失败：${error.javaClass.simpleName}" }
}

/** Convert FFmpeg output into the NAS's deterministic, strictly numbered SRT format. */
internal object SrtNormalization {
    fun normalize(value: String): String {
        require(value.toByteArray(Charsets.UTF_8).size <= 2 * 1024 * 1024) { "原字幕超过 2 MB，无法提交翻译" }
        val text = value.trimStart('\uFEFF').replace("\r\n", "\n").replace('\r', '\n').trim()
        require(!Regex("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]").containsMatchIn(text)) { "原字幕包含非法控制字符" }
        val timestamp = Regex("(\\d{1,3}):(\\d{2}):(\\d{2})[,.](\\d{3})")
        fun millis(stamp: String): Long {
            val groups = requireNotNull(timestamp.matchEntire(stamp)?.groupValues) { "原字幕时间戳无效" }
            val h = groups[1].toLong(); val m = groups[2].toLong(); val s = groups[3].toLong(); val ms = groups[4].toLong()
            require(m < 60 && s < 60) { "原字幕时间戳无效" }
            return ((h * 60 + m) * 60 + s) * 1000 + ms
        }
        data class Cue(val start: Long, val end: Long, val body: String)
        val cues = text.split(Regex("\n[ \t]*\n")).mapNotNull { block ->
            val lines = block.trim().lines().let { if (it.firstOrNull()?.trim()?.toIntOrNull() != null) it.drop(1) else it }
            require(lines.size >= 2) { "原字幕段落格式无效" }
            val stamps = lines[0].split("-->").map { it.trim().substringBefore(' ') }
            require(stamps.size == 2) { "原字幕时间戳无效" }
            val start = millis(stamps[0]); val end = millis(stamps[1])
            val body = lines.drop(1).joinToString("\n").trim()
            require(body.length <= 8000) { "单条字幕超过 8000 字符" }
            if (end <= start || body.replace(Regex("<[^>]*>"), "").isBlank()) null else Cue(start, end, body)
        }.sortedBy { it.start }
        require(cues.isNotEmpty() && cues.size <= 20000) { "字幕为空或超过 20000 条" }
        fun stamp(ms: Long) = "%02d:%02d:%02d,%03d".format(java.util.Locale.ROOT, ms / 3600000, ms / 60000 % 60, ms / 1000 % 60, ms % 1000)
        return cues.mapIndexed { index, cue -> "${index + 1}\n${stamp(cue.start)} --> ${stamp(cue.end)}\n${cue.body}\n\n" }.joinToString("").also {
            require(it.toByteArray(Charsets.UTF_8).size <= 2 * 1024 * 1024) { "规范化后的字幕超过 2 MB" }
        }
    }
}

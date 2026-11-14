package cn.qwensub.app

import android.content.Context
import org.json.JSONObject
import java.security.MessageDigest
import java.util.UUID

/** Persist before prepare: a lost HTTP reply must never enqueue a second paid translation. */
internal class MediaRequestStore(context: Context) {
    companion object { private val lock = Any() }
    private val preferences = context.getSharedPreferences("nas_media_requests", Context.MODE_PRIVATE)

    fun saved(settings: ConnectionSettings, mediaId: String): String? = synchronized(lock) {
        stored(settings, mediaId)?.optString("request_id")?.takeIf { it.isNotBlank() }
    }

    fun request(
        settings: ConnectionSettings, mediaId: String, subtitleId: String?, forceAsr: Boolean,
        audioId: Int?, replace: Boolean = false,
    ): String = synchronized(lock) {
        val payload = payload(mediaId, subtitleId, forceAsr, audioId)
        val prior = stored(settings, mediaId)
        if (!replace && prior?.optString("payload") == payload) {
            prior.optString("request_id").takeIf { it.isNotBlank() }?.let { return@synchronized it }
        }
        val id = UUID.randomUUID().toString()
        check(preferences.edit().putString(key(settings, mediaId), JSONObject().apply {
            put("request_id", id); put("payload", payload)
        }.toString()).commit()) { "无法保存字幕任务，请检查手机存储空间后重试。" }
        id
    }

    /** Reuse a job only when it was submitted for this exact selected text track. */
    fun matches(settings: ConnectionSettings, mediaId: String, requestId: String, subtitleId: String?): Boolean = synchronized(lock) {
        val previous = stored(settings, mediaId)
        previous != null && previous.optString("request_id") == requestId && previous.optString("payload") == payload(mediaId, subtitleId, false, null)
    }

    /** A cleanup acknowledgement must not erase a newer request created for the same media. */
    fun forget(requestId: String) = synchronized(lock) {
        val matching = preferences.all.filter { (_, value) ->
            (value as? String)?.let { runCatching { JSONObject(it).optString("request_id") == requestId }.getOrDefault(false) } == true
        }.keys
        if (matching.isEmpty()) return@synchronized
        check(preferences.edit().apply { matching.forEach { remove(it) } }.commit()) { "无法更新字幕任务记录，请稍后重试清理。" }
    }

    private fun payload(mediaId: String, subtitleId: String?, forceAsr: Boolean, audioId: Int?): String = JSONObject().apply {
        put("media_id", mediaId); put("subtitle_id", subtitleId ?: JSONObject.NULL)
        put("force_asr", forceAsr); put("audio_id", audioId ?: JSONObject.NULL)
    }.toString()

    private fun stored(settings: ConnectionSettings, mediaId: String): JSONObject? =
        preferences.getString(key(settings, mediaId), null)?.let { runCatching { JSONObject(it) }.getOrNull() }

    private fun key(settings: ConnectionSettings, mediaId: String): String = MessageDigest.getInstance("SHA-256")
        .digest((settings.baseUrl.trimEnd('/') + "\n" + mediaId).toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
}

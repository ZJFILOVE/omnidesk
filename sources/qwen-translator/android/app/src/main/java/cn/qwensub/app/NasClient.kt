package cn.qwensub.app

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.net.Proxy
import java.util.concurrent.TimeUnit

internal class NasException(message: String, val retryable: Boolean) : IOException(message)

/** NAS requests deliberately bypass the HTTP proxy configured for the video downloader. */
internal class NasClient(private val settings: ConnectionSettings) {
    companion object {
        private val http = OkHttpClient.Builder().proxy(Proxy.NO_PROXY)
            .connectTimeout(20, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS)
            .callTimeout(90, TimeUnit.SECONDS).followRedirects(false).followSslRedirects(false).build()
        private val jsonType = "application/json; charset=utf-8".toMediaType()
    }

    suspend fun health(): String {
        val result = JSONObject(call("GET", "/health"))
        if (result.optString("status") != "ok" || result.optInt("api_version") != 1) {
            throw NasException("飞牛接口版本不兼容，请更新字幕服务", false)
        }
        return "连接成功 · ${result.optString("model", "未知模型")}"
    }

    suspend fun submit(task: DownloadTask, srt: String): NasJob = job(JSONObject(call("POST", "/v1/jobs", JSONObject().apply {
        put("client_request_id", task.clientRequestId); put("title", task.title)
        put("source_url", task.url); put("subtitle_srt", srt)
    })))

    suspend fun status(id: String): NasJob = job(JSONObject(call("GET", "/v1/jobs/${safeId(id)}")))
    suspend fun retry(id: String): NasJob = job(JSONObject(call("POST", "/v1/jobs/${safeId(id)}/retry", JSONObject())))
    suspend fun cancel(id: String) { call("POST", "/v1/jobs/${safeId(id)}/cancel", JSONObject()) }

    /** A tombstone keyed by the original request also covers a POST whose reply was lost. */
    suspend fun deleteRequest(clientRequestId: String) {
        val id = safeId(clientRequestId)
        val health = try { JSONObject(call("GET", "/health")) }
        catch (error: org.json.JSONException) { throw NasException("飞牛清理接口不兼容，请更新字幕服务", false) }
        val capabilities = health.optJSONArray("capabilities")
        if (health.optString("status") != "ok" || health.optInt("api_version") != 1 ||
            capabilities == null || (0 until capabilities.length()).none { capabilities.optString(it) == "delete_requests" }) {
            throw NasException("飞牛服务尚不支持文件清理，请更新字幕服务后重试", false)
        }
        val result = try { JSONObject(call("DELETE", "/v1/requests/$id")) }
        catch (error: org.json.JSONException) { throw NasException("飞牛未确认文件已清理，请更新字幕服务后重试", false) }
        if (result.optString("status") != "deleted") throw NasException("飞牛未确认文件已清理，请更新字幕服务后重试", false)
    }
    suspend fun subtitle(id: String, type: String): String {
        require(type in setOf("original", "zh", "bilingual"))
        return call("GET", "/v1/jobs/${safeId(id)}/subtitles/$type", limit = 16 * 1024 * 1024)
    }

    private fun safeId(id: String): String {
        require(Regex("[A-Za-z0-9_-]{1,128}").matches(id)) { "飞牛返回的任务编号无效" }
        return id
    }

    private fun job(value: JSONObject): NasJob = NasJob(
        id = safeId(value.getString("id")), state = value.getString("state"),
        progress = value.optDouble("progress", 0.0).toFloat().coerceIn(0f, 1f),
        message = value.optString("message").take(300), model = value.optString("model"),
    )

    private suspend fun call(method: String, path: String, body: JSONObject? = null, limit: Int = 1024 * 1024): String = withContext(Dispatchers.IO) {
        if (settings.baseUrl.isBlank() || settings.token.isBlank()) throw NasException("请在连接设置填写飞牛地址和访问令牌", false)
        val request = Request.Builder().url(settings.baseUrl + path)
            .header("Authorization", "Bearer ${settings.token}").header("Accept", "application/json, text/plain")
            .method(method, body?.toString()?.toRequestBody(jsonType)).build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                val description = when (response.code) {
                    401, 403 -> "飞牛访问令牌无效或无权限，请更新连接设置"
                    404 -> "飞牛找不到此任务或接口，请检查服务地址"
                    413 -> "字幕文件超过飞牛允许的大小"
                    429 -> "飞牛任务队列繁忙，稍后自动重试"
                    in 500..599 -> "飞牛服务暂时不可用，稍后自动重试"
                    else -> "飞牛请求失败（HTTP ${response.code}）"
                }
                throw NasException(description, response.code == 408 || response.code == 429 || response.code >= 500)
            }
            val source = response.body?.source() ?: throw NasException("飞牛返回了空响应", true)
            if (source.request(limit.toLong() + 1)) throw NasException("飞牛响应超过大小限制", false)
            source.readUtf8()
        }
    }
}

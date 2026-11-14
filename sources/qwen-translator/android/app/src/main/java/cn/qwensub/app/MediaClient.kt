package cn.qwensub.app

import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.ConnectionPool
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.Proxy
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID
import java.util.concurrent.TimeUnit

/** All Arr credentials and mounted paths stay on the NAS. Only its authenticated API is used. */
internal class MediaClient(private val settings: ConnectionSettings) {
    internal val selectionScope: String get() = settings.baseUrl.trim().trimEnd('/')
    companion object {
        private val readHttp = OkHttpClient.Builder().proxy(Proxy.NO_PROXY)
            .connectTimeout(20, TimeUnit.SECONDS).readTimeout(90, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true).followRedirects(false).followSslRedirects(false).build()
        // Fresh connections avoid expired NAS keep-alive sockets without replaying a mutation.
        private val writeHttp = readHttp.newBuilder().retryOnConnectionFailure(false)
            .connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS)).build()
        private val jsonType = "application/json; charset=utf-8".toMediaType()
    }

    suspend fun catalog(): NasMediaCatalog {
        val value = JSONObject(call("GET", "/v1/media/catalog"))
        return NasMediaCatalog(value.optBoolean("configured"), value.optJSONArray("items").objects().map(::title),
            value.optJSONArray("services").objects().map { NasMediaService(it.optString("kind"), it.optBoolean("connected"), it.optString("message")) },
            value.optJSONArray("capabilities")?.let { array ->
                (0 until array.length()).mapNotNull { array.opt(it) as? String }.toSet()
            }.orEmpty())
    }

    suspend fun discover(kind: String, query: String): List<NasDiscoveredTitle> {
        require(kind in setOf("sonarr", "radarr"))
        return JSONObject(call("GET", "/v1/media/discover", query = mapOf("kind" to kind, "query" to query.trim())))
            .optJSONArray("items").objects().map {
                NasDiscoveredTitle(it.getInt("external_id"), it.getString("kind"), it.optString("title"), it.optInt("year").takeIf { value -> value > 0 },
                    it.optString("overview"), it.optBoolean("already_added"), it.nullable("title_id"))
            }
    }

    suspend fun options(kind: String): NasMediaAddOptions {
        require(kind in setOf("sonarr", "radarr"))
        val value = JSONObject(call("GET", "/v1/media/services/$kind/options"))
        fun options(key: String) = value.optJSONArray(key).objects().map { NasMediaOption(it.getInt("id"), it.optString("name"), it.optString("description")) }
        return NasMediaAddOptions(options("root_folders"), options("quality_profiles"),
            value.optInt("default_quality_profile_id").takeIf { it > 0 })
    }

    suspend fun add(kind: String, externalId: Int, qualityProfileId: Int, rootFolderId: Int): NasMediaTitle {
        require(kind in setOf("sonarr", "radarr"))
        val value = JSONObject(call("POST", "/v1/media/services/$kind/add", JSONObject().apply {
            put("external_id", externalId); put("quality_profile_id", qualityProfileId); put("root_folder_id", rootFolderId)
            put("monitored", true); put("search", true)
        }))
        return title(value).copy(searchMessage = value.nullable("search_message"))
    }

    suspend fun title(id: String): NasMediaTitle = title(JSONObject(call("GET", "/v1/media/titles/${safeId(id)}")))
    suspend fun file(id: String): NasMediaFile {
        val value = JSONObject(call("GET", "/v1/media/files/${safeId(id)}"))
        return NasMediaFile(
            id = safeId(value.getString("id")), title = value.optString("title"), filename = value.optString("filename"),
            size = value.optLong("size").coerceAtLeast(0), streamPath = validPath(value.getString("stream_path")),
            subtitles = value.optJSONArray("subtitles").objects().map { track ->
                NasMediaSubtitle(safeId(track.getString("id")), track.optString("title"), track.optString("language", "und"),
                    track.optString("source"), validPath(track.getString("download_path")))
            },
            duration = value.optDouble("duration", 0.0).takeIf { it.isFinite() }?.coerceAtLeast(0.0) ?: 0.0,
            audio = value.optJSONArray("audio").objects().map { NasMediaAudio(it.getInt("id"), it.optString("title"), it.optString("language", "und")) },
            job = value.optJSONObject("job")?.let(::job), asrAvailable = value.optBoolean("asr_available"),
            compatible = value.optJSONObject("compatible")?.let(::compatibility),
            compatibleAvailable = value.optBoolean("compatible_available"),
            compatibleStreamPath = value.nullable("compatible_stream_path")?.let(::validPath),
        )
    }

    suspend fun compatibility(mediaId: String): NasMediaCompatibility = compatibility(
        JSONObject(call("GET", "/v1/media/files/${safeId(mediaId)}/compatible")))

    suspend fun prepareCompatible(mediaId: String): NasMediaCompatibility = compatibility(
        JSONObject(call("POST", "/v1/media/files/${safeId(mediaId)}/compatible", JSONObject())))

    suspend fun queue(): List<NasMediaQueueItem> = JSONObject(call("GET", "/v1/media/queue")).optJSONArray("items").objects().map {
        NasMediaQueueItem(safeId(it.getString("id")), it.optString("title"), it.optString("status"), progress(it),
            it.optLong("size").coerceAtLeast(0), it.optLong("size_left").coerceAtLeast(0), it.optString("time_left"), it.optString("kind"))
    }

    suspend fun removeQueueItem(id: String, blocklist: Boolean = false) {
        val checkedId = safeId(id)
        val response = call("POST", "/v1/media/queue/$checkedId/remove", JSONObject().put("blocklist", blocklist))
        confirmRemoval(response, checkedId, "飞牛未确认下载任务已删除，请刷新队列后重试。")
    }

    suspend fun setMonitored(id: String, monitored: Boolean): NasMediaTitle {
        val checkedId = safeId(id)
        val response = call("POST", "/v1/media/titles/$checkedId/monitor", JSONObject().put("monitored", monitored))
        val value = try { JSONObject(response) } catch (_: org.json.JSONException) {
            throw NasException("飞牛未确认自动追更设置已更新，请刷新详情后重试。", false)
        }
        if (value.optString("id") != checkedId || value.opt("monitored") != monitored) {
            throw NasException("飞牛未确认自动追更设置已更新，请刷新详情后重试。", false)
        }
        return title(value)
    }

    suspend fun removeTitle(id: String, deleteFiles: Boolean = false, addImportExclusion: Boolean = true) {
        val checkedId = safeId(id)
        val response = call("POST", "/v1/media/titles/$checkedId/remove", JSONObject().apply {
            put("delete_files", deleteFiles); put("add_import_exclusion", addImportExclusion)
        }, forbiddenMessage = if (deleteFiles) "此作品目录未获删除授权，可保留文件移除，或检查飞牛媒体目录配置。" else null)
        confirmRemoval(response, checkedId, "飞牛未确认作品已移除，请刷新片库后重试。")
    }

    private fun confirmRemoval(response: String, id: String, message: String) {
        val value = try { JSONObject(response) } catch (_: org.json.JSONException) { throw NasException(message, false) }
        if (value.optString("status") != "removed" || value.optString("id") != id) throw NasException(message, false)
    }

    suspend fun search(titleId: String): String = JSONObject(call("POST", "/v1/media/titles/${safeId(titleId)}/search", JSONObject()))
        .optString("message").ifBlank { "已提交下载搜索，可在下载队列中查看进度。" }

    suspend fun prepare(
        mediaId: String, subtitleId: String? = null, forceAsr: Boolean = false, audioId: Int? = null,
        requestId: String = UUID.randomUUID().toString(),
    ): NasMediaJob = job(JSONObject(call("POST", "/v1/media/files/${safeId(mediaId)}/prepare", JSONObject().apply {
        put("client_request_id", requestId); put("subtitle_id", if (forceAsr) JSONObject.NULL else subtitleId ?: JSONObject.NULL)
        put("force_asr", forceAsr); put("audio_id", audioId ?: JSONObject.NULL)
    })))

    suspend fun job(id: String): NasMediaJob = job(JSONObject(call("GET", "/v1/media/jobs/${safeId(id)}")))
    suspend fun retry(id: String): NasMediaJob = job(JSONObject(call("POST", "/v1/media/jobs/${safeId(id)}/retry", JSONObject())))
    suspend fun cancel(id: String) { call("POST", "/v1/media/jobs/${safeId(id)}/cancel", JSONObject()) }
    suspend fun text(path: String): String = call("GET", validPath(path), limit = 16 * 1024 * 1024)
    suspend fun subtitle(mediaId: String, trackId: String): String = text("/v1/media/files/${safeId(mediaId)}/subtitles/${safeId(trackId)}")
    suspend fun jobSubtitle(jobId: String, kind: String): String {
        require(kind in setOf("original", "zh", "bilingual"))
        return text("/v1/media/jobs/${safeId(jobId)}/subtitles/$kind")
    }
    suspend fun downloadVideo(mediaId: String, target: File, progress: (Float) -> Unit) =
        download("/v1/media/files/${safeId(mediaId)}/stream", target, progress)

    suspend fun deleteRequest(id: String) {
        val response = call("DELETE", "/v1/media/requests/${safeId(id)}")
        val result = try { JSONObject(response) } catch (_: org.json.JSONException) {
            throw NasException("飞牛未确认字幕文件已清理，请更新服务后重试。", false)
        }
        if (result.optString("status") != "deleted") throw NasException("飞牛未确认字幕文件已清理，请更新服务后重试。", false)
    }

    /** Resume only a byte-identical representation, verified by a strong ETag and Content-Range. */
    suspend fun download(path: String, target: File, progress: (Float) -> Unit) = withContext(Dispatchers.IO) {
        val partial = File(target.parentFile, target.name + ".part")
        val validator = File(target.parentFile, target.name + ".etag")
        val savedEtag = validator.takeIf { it.isFile }?.readText()?.takeIf(::strongEtag)
        val offset = if (savedEtag != null && partial.isFile) partial.length() else 0L
        val builder = request("GET", validPath(path), null).newBuilder().header("Accept-Encoding", "identity")
        if (offset > 0) builder.header("Range", "bytes=$offset-").header("If-Range", savedEtag!!)
        useResponse(builder.build()) { response ->
            val returnedEtag = response.header("ETag")?.takeIf(::strongEtag)
            if (response.code == 416) {
                val total = Regex("""bytes \*/(\d+)""").matchEntire(response.header("Content-Range").orEmpty())?.groupValues?.get(1)?.toLongOrNull()
                if (offset > 0 && total == offset && returnedEtag == savedEtag) {
                    finishDownload(partial, target, validator); progress(1f); return@useResponse
                }
                partial.delete(); validator.delete()
                throw NasException("下载文件已变化，将在重试时重新下载。", true)
            }
            if (!response.isSuccessful) throw responseError(response.code)
            val body = response.body ?: throw NasException("飞牛返回了空文件", true)
            val append: Boolean
            val total: Long
            if (response.code == 206) {
                val range = Regex("""bytes (\d+)-(\d+)/(\d+)""").matchEntire(response.header("Content-Range").orEmpty())
                    ?: throw NasException("飞牛返回的断点下载范围无效。", true)
                val start = range.groupValues[1].toLongOrNull()
                val end = range.groupValues[2].toLongOrNull()
                val size = range.groupValues[3].toLongOrNull()
                if (offset <= 0 || start != offset || end == null || size == null || end < offset || size <= end ||
                    returnedEtag != savedEtag || (body.contentLength() >= 0 && body.contentLength() != end - offset + 1)) {
                    partial.delete(); validator.delete()
                    throw NasException("飞牛返回的断点下载范围不匹配，请重试。", true)
                }
                append = true; total = size
            } else if (response.code == 200) {
                append = false; total = body.contentLength()
            } else throw NasException("飞牛返回了不支持的媒体下载响应。", true)
            partial.parentFile?.mkdirs()
            var count = if (append) offset else 0L
            body.byteStream().use { input ->
                java.io.FileOutputStream(partial, append).use { output ->
                    // Truncate an old representation before persisting its replacement validator.
                    if (returnedEtag != null) validator.writeText(returnedEtag) else validator.delete()
                    val buffer = ByteArray(256 * 1024)
                    while (true) {
                        ensureActive()
                        val read = input.read(buffer)
                        if (read < 0) break
                        output.write(buffer, 0, read); count += read
                        if (total > 0) progress((count.toDouble() / total).toFloat().coerceIn(0f, 1f))
                    }
                    output.fd.sync()
                }
            }
            ensureActive()
            if (count == 0L || (total >= 0 && count != total)) throw IOException("视频下载中断，请重试。")
            finishDownload(partial, target, validator)
            progress(1f)
        }
    }

    private fun strongEtag(value: String): Boolean = value.length in 2..512 && value.startsWith('"') && value.endsWith('"') && !value.any { it.code < 32 }

    private fun finishDownload(partial: File, target: File, validator: File) {
        try { Files.move(partial.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE) }
        catch (_: AtomicMoveNotSupportedException) { Files.move(partial.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING) }
        validator.delete()
    }

    /** The cancellation watcher closes both connect/read operations, including a blocked stream read. */
    private suspend fun <T> useResponse(request: Request, consume: suspend (Response) -> T): T = coroutineScope {
        ensureActive()
        val http = if (request.method == "GET" || request.method == "HEAD") readHttp else writeHttp
        val call = http.newCall(request)
        val watcher = launch(start = CoroutineStart.UNDISPATCHED) { try { awaitCancellation() } finally { call.cancel() } }
        try {
            call.execute().use { consume(it) }
        } catch (failure: IOException) {
            // Cancelling the socket wakes execute/read as IOException; preserve coroutine cancellation.
            ensureActive()
            throw failure
        } finally {
            watcher.cancel()
        }
    }

    fun resolvePath(path: String): String {
        require(settings.baseUrl.isNotBlank() && settings.token.isNotBlank()) { "请先在连接设置填写飞牛地址和访问令牌。" }
        return settings.baseUrl.trimEnd('/') + validPath(path)
    }

    private fun validPath(path: String): String {
        require(path.startsWith("/v1/media/") && !path.contains("..") && !path.contains('\\') &&
            !path.contains('%') && !path.contains('?') && !path.contains('#') && !path.any { it.isWhitespace() || it.code < 32 }) {
            "飞牛返回了无效的媒体文件地址，请更新服务。"
        }
        return path
    }

    private fun request(method: String, path: String, body: JSONObject?, query: Map<String, String> = emptyMap()): Request = Request.Builder()
        .url(resolvePath(path).toHttpUrl().newBuilder().apply { query.forEach { (key, value) -> addQueryParameter(key, value) } }.build()).header("Authorization", "Bearer ${settings.token}")
        .header("Accept", "application/json, text/plain, application/octet-stream")
        .method(method, body?.toString()?.toRequestBody(jsonType)).build()

    private suspend fun call(
        method: String, path: String, body: JSONObject? = null, limit: Int = 8 * 1024 * 1024,
        query: Map<String, String> = emptyMap(), forbiddenMessage: String? = null,
    ): String = withContext(Dispatchers.IO) {
        useResponse(request(method, path, body, query)) { response ->
            if (!response.isSuccessful) throw responseError(response.code, forbiddenMessage)
            val source = response.body?.source() ?: throw NasException("飞牛返回了空响应", true)
            if (source.request(limit.toLong() + 1)) throw NasException("飞牛片库响应超过大小限制", false)
            source.readUtf8()
        }
    }

    private fun responseError(code: Int, forbiddenMessage: String? = null): NasException {
        val message = when (code) {
            401 -> "访问令牌无效或无权限，请检查连接设置。"
            403 -> forbiddenMessage ?: "访问令牌无效或无权限，请检查连接设置。"
            404 -> "飞牛找不到媒体或片库接口，请刷新片库，并确认已更新飞牛服务。"
            409 -> "文件或字幕正在处理中，请稍后刷新。"
            422 -> "无法处理所选字幕或音轨，请重新选择。"
            429 -> "飞牛任务较多，请稍后重试。"
            503 -> "飞牛媒体服务尚未配置或暂时无法连接，请检查 Radarr / Sonarr 和媒体目录。"
            in 500..599 -> "飞牛媒体服务暂时不可用，请稍后重试。"
            else -> "飞牛请求失败（HTTP $code）。"
        }
        return NasException(message, code == 408 || code == 429 || code >= 500)
    }

    private fun title(value: JSONObject): NasMediaTitle = NasMediaTitle(
        id = safeId(value.getString("id")), kind = value.optString("kind"), title = value.optString("title"),
        year = value.optInt("year").takeIf { it > 0 }, overview = value.optString("overview"), monitored = value.optBoolean("monitored"),
        availableCount = value.optInt("available_count"), totalCount = value.optInt("total_count"), mediaId = value.nullable("media_id")?.let(::safeId),
        searchMessage = value.nullable("search_message"),
        episodes = value.optJSONArray("episodes").objects().map { episode ->
            NasMediaEpisode(safeId(episode.getString("id")), episode.optString("title"), episode.optInt("season_number"),
                episode.optInt("episode_number"), episode.optBoolean("has_file"), episode.optBoolean("monitored"), episode.nullable("media_id")?.let(::safeId))
        },
    )

    private fun job(value: JSONObject): NasMediaJob = NasMediaJob(safeId(value.getString("id")), value.optString("state"), progress(value),
        value.optString("message").take(500), value.optString("model"), value.optString("media_id"), value.nullable("client_request_id"))

    private fun compatibility(value: JSONObject): NasMediaCompatibility = NasMediaCompatibility(
        state = value.optString("state"), progress = progress(value), message = value.optString("message").take(500),
        streamPath = value.nullable("stream_path")?.let(::validPath),
        size = if (value.isNull("size")) null else value.optLong("size").takeIf { it >= 0 },
    )

    private fun progress(value: JSONObject): Float = value.optDouble("progress", 0.0).takeIf { it.isFinite() }?.toFloat()?.coerceIn(0f, 1f) ?: 0f
    private fun safeId(id: String): String {
        require(Regex("[A-Za-z0-9_:-]{1,160}").matches(id)) { "飞牛返回了无效的媒体编号。" }
        return id
    }
}

private fun JSONArray?.objects(): List<JSONObject> = if (this == null) emptyList() else (0 until length()).map { getJSONObject(it) }
private fun JSONObject.nullable(key: String): String? = if (isNull(key)) null else optString(key).takeIf { it.isNotBlank() }

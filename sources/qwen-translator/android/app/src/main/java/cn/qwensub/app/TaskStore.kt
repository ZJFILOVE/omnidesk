package cn.qwensub.app

import android.content.Context
import android.util.AtomicFile
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** One process-wide store; each mutation is persisted before it is published to Compose. */
internal class TaskStore(context: Context) {
    private val file = AtomicFile(File(context.filesDir, "tasks-v1.json"))
    private val mutableTasks = MutableStateFlow(read())
    val tasks: StateFlow<List<DownloadTask>> = mutableTasks.asStateFlow()

    init {
        // Upgrade old completed deletions and finish a cleanup interrupted before finalization.
        val migrated = mutableTasks.value.mapNotNull { finalizedCleanup(it) }
        if (migrated != mutableTasks.value) persist(migrated)
    }

    @Synchronized fun get(id: String): DownloadTask? = mutableTasks.value.find { it.id == id }

    @Synchronized fun add(task: DownloadTask) = persist(listOf(task) + mutableTasks.value)

    @Synchronized fun update(id: String, transform: (DownloadTask) -> DownloadTask): DownloadTask? {
        val previous = get(id) ?: return null
        val transformed = transform(previous)
        // A retry can retain the earlier error until its last deletion succeeds. Clear that
        // error in the same transaction as the last pending flag, even if the worker then dies.
        val cleanupCompleted = previous.cleanupPending && !transformed.cleanupPending &&
            (!previous.localCleanupPending || transformed.localFilesDeleted) &&
            (!previous.remoteCleanupPending || transformed.remoteFilesDeleted)
        val updated = transformed.copy(updatedAt = System.currentTimeMillis(),
            cleanupError = if (cleanupCompleted) null else transformed.cleanupError)
        if (previous.copy(updatedAt = updated.updatedAt) == updated) return previous
        persist(mutableTasks.value.map { if (it.id == id) updated else it })
        return updated
    }

    /** Keep metadata for the other storage location, but remove completed cleanup from history. */
    @Synchronized fun finalizeCleanup(
        id: String,
        expectedVideoGeneration: Int? = null,
        expectedSubtitleGeneration: Int? = null,
    ): DownloadTask? {
        val previous = get(id) ?: return null
        // A download retry or a newer cleanup request supersedes this worker's completion.
        if (expectedVideoGeneration != null && previous.videoGeneration != expectedVideoGeneration ||
            expectedSubtitleGeneration != null && previous.subtitleGeneration != expectedSubtitleGeneration) return previous
        val updated = finalizedCleanup(previous)
        if (updated == previous) return previous
        persist(mutableTasks.value.mapNotNull { if (it.id == id) updated else it })
        return updated
    }

    private fun finalizedCleanup(task: DownloadTask): DownloadTask? {
        if (task.cleanupHistoryHandled || task.cleanupPending || task.cleanupError != null ||
            (!task.localFilesDeleted && !task.remoteFilesDeleted)) return task
        // nasBaseUrl is persisted before POST, even when its response is lost. No binding means
        // this task never reached the NAS; otherwise retain its request ID until remote deletion.
        val remoteAbsent = task.remoteFilesDeleted || task.nasMediaId != null && task.remoteId == null ||
            task.nasBaseUrl == null && task.remoteId == null
        if (task.localFilesDeleted && remoteAbsent) return null
        // Remote-only cleanup must not remove the controls for an unfinished video download.
        if (!task.localFilesDeleted &&
            (task.videoState != TaskState.READY || task.videoPath?.let { File(it).isFile } != true)) return task
        return task.copy(historyHidden = true, cleanupHistoryHandled = true, updatedAt = System.currentTimeMillis())
    }

    private fun persist(tasks: List<DownloadTask>) {
        val array = JSONArray().apply { tasks.forEach { put(encode(it)) } }
        val stream = file.startWrite()
        try {
            stream.write(array.toString().toByteArray(Charsets.UTF_8))
            file.finishWrite(stream)
        } catch (error: Throwable) {
            file.failWrite(stream)
            throw error
        }
        mutableTasks.value = tasks
    }

    private fun read(): List<DownloadTask> {
        if (!file.baseFile.exists() && !File(file.baseFile.path + ".bak").exists()) return emptyList()
        // Preserve damaged state for diagnostics instead of overwriting it with an empty list.
        return try {
            val array = JSONArray(file.openRead().bufferedReader().use { it.readText() })
            (0 until array.length()).map { decode(array.getJSONObject(it)) }
        } catch (error: Exception) {
            throw IllegalStateException("任务记录无法读取，请先保留应用数据再排查", error)
        }
    }

    private fun encode(t: DownloadTask) = JSONObject().apply {
        put("id", t.id); put("title", t.title); put("url", t.url)
        put("videoState", t.videoState.name); put("subtitleState", t.subtitleState.name)
        put("videoProgress", t.videoProgress.toDouble()); put("subtitleProgress", t.subtitleProgress.toDouble())
        put("videoPath", t.videoPath); put("originalPath", t.originalPath); put("zhPath", t.zhPath); put("bilingualPath", t.bilingualPath)
        put("remoteId", t.remoteId); put("videoError", t.videoError); put("subtitleError", t.subtitleError)
        put("createdAt", t.createdAt); put("updatedAt", t.updatedAt); put("clientRequestId", t.clientRequestId)
        put("remoteRetryRequested", t.remoteRetryRequested); put("nasBaseUrl", t.nasBaseUrl)
        put("videoGeneration", t.videoGeneration); put("subtitleGeneration", t.subtitleGeneration)
        put("submissionTitle", t.submissionTitle)
        put("localCleanupPending", t.localCleanupPending); put("remoteCleanupPending", t.remoteCleanupPending)
        put("localFilesDeleted", t.localFilesDeleted); put("remoteFilesDeleted", t.remoteFilesDeleted)
        put("cleanupError", t.cleanupError)
        put("historyHidden", t.historyHidden); put("cleanupHistoryHandled", t.cleanupHistoryHandled)
        put("nasMediaId", t.nasMediaId); put("nasStreamPath", t.nasStreamPath); put("nasSubtitleId", t.nasSubtitleId)
        put("nasOriginalPath", t.nasOriginalPath); put("nasZhPath", t.nasZhPath); put("nasBilingualPath", t.nasBilingualPath)
    }

    private fun decode(o: JSONObject) = DownloadTask(
        id = o.getString("id"), title = o.getString("title"), url = o.getString("url"),
        videoState = TaskState.valueOf(o.getString("videoState")), subtitleState = TaskState.valueOf(o.getString("subtitleState")),
        videoProgress = o.optDouble("videoProgress", 0.0).toFloat(), subtitleProgress = o.optDouble("subtitleProgress", 0.0).toFloat(),
        videoPath = o.nullable("videoPath"), originalPath = o.nullable("originalPath"), zhPath = o.nullable("zhPath"), bilingualPath = o.nullable("bilingualPath"),
        remoteId = o.nullable("remoteId"), videoError = o.nullable("videoError"), subtitleError = o.nullable("subtitleError"),
        createdAt = o.getLong("createdAt"), updatedAt = o.getLong("updatedAt"), clientRequestId = o.getString("clientRequestId"),
        remoteRetryRequested = o.optBoolean("remoteRetryRequested"), nasBaseUrl = o.nullable("nasBaseUrl"),
        videoGeneration = o.optInt("videoGeneration"), subtitleGeneration = o.optInt("subtitleGeneration"),
        submissionTitle = o.nullable("submissionTitle"),
        localCleanupPending = o.optBoolean("localCleanupPending"), remoteCleanupPending = o.optBoolean("remoteCleanupPending"),
        localFilesDeleted = o.optBoolean("localFilesDeleted"), remoteFilesDeleted = o.optBoolean("remoteFilesDeleted"),
        cleanupError = o.nullable("cleanupError"),
        historyHidden = o.optBoolean("historyHidden"), cleanupHistoryHandled = o.optBoolean("cleanupHistoryHandled"),
        nasMediaId = o.nullable("nasMediaId"), nasStreamPath = o.nullable("nasStreamPath"), nasSubtitleId = o.nullable("nasSubtitleId"),
        nasOriginalPath = o.nullable("nasOriginalPath"), nasZhPath = o.nullable("nasZhPath"), nasBilingualPath = o.nullable("nasBilingualPath"),
    )
    private fun JSONObject.nullable(key: String): String? = if (isNull(key)) null else optString(key).takeIf { it.isNotBlank() }
}

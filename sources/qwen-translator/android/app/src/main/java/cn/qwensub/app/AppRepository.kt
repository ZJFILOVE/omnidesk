package cn.qwensub.app

import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.workDataOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit
import java.util.UUID

class AppRepository private constructor(context: Context) {
    companion object {
        @Volatile private var instance: AppRepository? = null
        fun get(context: Context): AppRepository = instance ?: synchronized(this) {
            instance ?: AppRepository(context.applicationContext).also { instance = it }
        }
    }

    internal val context = context.applicationContext
    internal val store = TaskStore(this.context)
    private val settingsStore = SettingsStore(this.context)
    private val work get() = WorkManager.getInstance(context)
    val tasks: StateFlow<List<DownloadTask>> = store.tasks

    fun settings(): ConnectionSettings = settingsStore.read()

    fun saveSettings(value: ConnectionSettings) {
        settingsStore.write(value)
        // A changed route/credential should wake jobs currently waiting for the NAS.
        tasks.value.filter { it.subtitleState == TaskState.WAITING }.forEach { retrySubtitles(it.id) }
        tasks.value.filter { it.subtitleState == TaskState.CANCELLED && it.nasBaseUrl != null && it.subtitleError != null }
            .forEach { enqueueRemoteCancel(it) }
        tasks.value.filter { it.cleanupPending }.forEach { enqueueCleanup(it, ExistingWorkPolicy.REPLACE) }
    }

    fun submit(url: String): String {
        val task = DownloadTask(url = InputValidation.youtubeUrl(url))
        store.add(task)
        enqueueSubtitles(task, ExistingWorkPolicy.KEEP)
        enqueueVideo(task, ExistingWorkPolicy.KEEP)
        return task.id
    }

    internal fun submitNasMedia(playback: NasPlayback): String {
        val settings = settings()
        require(settings.baseUrl == playback.settings.baseUrl) { "飞牛连接已改变，请重新打开 NAS 片库" }
        val client = MediaClient(settings)
        client.resolvePath(playback.file.streamPath)
        val existing = tasks.value.firstOrNull {
            it.nasMediaId == playback.file.id && it.nasBaseUrl == settings.baseUrl &&
                it.nasStreamPath == playback.file.streamPath &&
                !it.localFilesDeleted && !it.cleanupPending &&
                it.videoState in setOf(TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING, TaskState.READY)
        }
        val selected = playback.subtitles.firstOrNull { it.id == playback.selectedSubtitleId }
            ?: playback.subtitles.firstOrNull()
        val selectedJobId = selected?.downloadPath?.let {
            Regex("/v1/media/jobs/([A-Za-z0-9-]+)/subtitles/(original|zh|bilingual)").matchEntire(it)?.groupValues?.get(1)
        }
        // Copy the captions the viewer selected. Reuse a prepared job only with its proven identity.
        val remote = playback.file.job?.takeIf { it.id == selectedJobId && it.mediaId == playback.file.id && it.clientRequestId != null }
        fun generated(kind: String) = playback.subtitles.firstOrNull {
            it.downloadPath == "/v1/media/jobs/$selectedJobId/subtitles/$kind"
        }?.downloadPath
        val originalPath = if (selectedJobId == null) selected?.downloadPath else generated("original") ?: selected?.downloadPath
        if (existing != null) {
            // Captions may finish after the video. Attach them without downloading gigabytes again.
            if (existing.remoteId == null && !existing.cleanupPending && selected != null &&
                (existing.nasOriginalPath != originalPath || existing.remoteFilesDeleted ||
                    existing.subtitleState in setOf(TaskState.SKIPPED, TaskState.FAILED, TaskState.CANCELLED))) {
                val updated = store.update(existing.id) { it.copy(
                    nasSubtitleId = selected.takeUnless { choice -> choice.source == "generated" }?.id,
                    nasOriginalPath = originalPath, nasZhPath = generated("zh"), nasBilingualPath = generated("bilingual"),
                    remoteId = remote?.id, clientRequestId = remote?.clientRequestId ?: UUID.randomUUID().toString(),
                    subtitleState = TaskState.QUEUED, subtitleError = null, subtitleProgress = 0f,
                    originalPath = null, zhPath = null, bilingualPath = null,
                    subtitleGeneration = it.subtitleGeneration + 1, remoteFilesDeleted = false, remoteRetryRequested = false,
                    historyHidden = false,
                ) } ?: return existing.id
                work.cancelUniqueWork("remote-cancel-${updated.id}")
                enqueueSubtitles(updated, ExistingWorkPolicy.REPLACE)
            }
            return existing.id
        }
        val task = DownloadTask(
            title = playback.file.title + if (playback.file.streamPath.endsWith("/compatible/stream")) "（兼容版）" else "",
            url = client.resolvePath(playback.file.streamPath),
            nasBaseUrl = settings.baseUrl, nasMediaId = playback.file.id, nasStreamPath = playback.file.streamPath,
            nasSubtitleId = selected?.takeUnless { it.source == "generated" }?.id,
            nasOriginalPath = originalPath,
            nasZhPath = generated("zh"), nasBilingualPath = generated("bilingual"),
            remoteId = remote?.id,
            clientRequestId = remote?.clientRequestId ?: UUID.randomUUID().toString(),
            subtitleState = if (playback.subtitles.isEmpty()) TaskState.SKIPPED else TaskState.QUEUED,
            subtitleError = if (playback.subtitles.isEmpty()) "未找到可用字幕，已跳过字幕处理；视频仍可观看。" else null,
        )
        store.add(task)
        enqueueVideo(task, ExistingWorkPolicy.KEEP)
        enqueueSubtitles(task, ExistingWorkPolicy.KEEP)
        return task.id
    }

    @Synchronized fun retryVideo(id: String) {
        if (store.get(id)?.cleanupPending != false) return
        val task = store.update(id) {
            if (it.videoState == TaskState.READY) it else it.copy(videoState = TaskState.QUEUED,
                videoError = null, videoGeneration = it.videoGeneration + 1, localFilesDeleted = false, cleanupError = null,
                historyHidden = false, cleanupHistoryHandled = true)
        } ?: return
        if (task.videoState == TaskState.READY) return
        DownloadEngine.stop("video-$id-${task.videoGeneration - 1}")
        enqueueVideo(task, ExistingWorkPolicy.REPLACE)
    }

    @Synchronized fun retrySubtitles(id: String) {
        if (store.get(id)?.cleanupPending != false) return
        val task = store.update(id) {
            if (it.subtitleState == TaskState.READY && !it.remoteFilesDeleted) it else it.copy(subtitleState = TaskState.QUEUED,
                subtitleError = null, subtitleGeneration = it.subtitleGeneration + 1,
                remoteRetryRequested = !it.remoteFilesDeleted, localFilesDeleted = false, remoteFilesDeleted = false,
                clientRequestId = if (it.remoteFilesDeleted) UUID.randomUUID().toString() else it.clientRequestId,
                remoteId = if (it.remoteFilesDeleted) null else it.remoteId,
                nasBaseUrl = if (it.remoteFilesDeleted && it.nasMediaId == null) null else it.nasBaseUrl,
                submissionTitle = if (it.remoteFilesDeleted) null else it.submissionTitle, cleanupError = null,
                nasOriginalPath = if (it.remoteFilesDeleted && it.nasOriginalPath?.startsWith("/v1/media/jobs/") == true) null else it.nasOriginalPath,
                nasZhPath = if (it.remoteFilesDeleted) null else it.nasZhPath,
                nasBilingualPath = if (it.remoteFilesDeleted) null else it.nasBilingualPath,
                historyHidden = false, cleanupHistoryHandled = true)
        } ?: return
        if (task.subtitleState == TaskState.READY) return
        work.cancelUniqueWork("remote-cancel-$id")
        DownloadEngine.stop("subtitle-$id-${task.subtitleGeneration - 1}")
        enqueueSubtitles(task, ExistingWorkPolicy.REPLACE)
    }

    @Synchronized fun cancelVideo(id: String) {
        if (store.get(id)?.cleanupPending != false) return
        val task = store.update(id) {
            if (it.videoState == TaskState.READY) it else it.copy(videoState = TaskState.CANCELLED,
                videoError = null, videoGeneration = it.videoGeneration + 1)
        } ?: return
        if (task.videoState == TaskState.READY) return
        DownloadEngine.stop("video-$id-${task.videoGeneration - 1}")
        work.cancelUniqueWork("video-$id")
    }

    @Synchronized fun cancelSubtitles(id: String) {
        if (store.get(id)?.subtitleCleanupBlocked != false) return
        val task = store.update(id) {
            if (it.subtitleState == TaskState.READY) it else it.copy(subtitleState = TaskState.CANCELLED,
                subtitleError = "已停止手机字幕任务；正在同步取消飞牛任务", subtitleGeneration = it.subtitleGeneration + 1)
        } ?: return
        if (task.subtitleState == TaskState.READY) return
        DownloadEngine.stop("subtitle-$id-${task.subtitleGeneration - 1}")
        work.cancelUniqueWork("subtitle-$id")
        if (task.remoteId != null || task.nasBaseUrl != null) enqueueRemoteCancel(task)
        else store.update(id) { it.copy(subtitleError = null) }
    }

    suspend fun testConnection(): String = NasClient(settings()).health()

    /** Persist a deletion outbox before stopping work, so process death cannot lose the request. */
    @Synchronized fun requestCleanup(ids: List<String>, local: Boolean, remote: Boolean) {
        if (!local && !remote) return
        ids.distinct().forEach { id ->
            val task = store.update(id) { previous ->
                val clearLocal = local && !previous.localFilesDeleted
                val clearRemote = remote && !previous.remoteFilesDeleted
                if (!clearLocal && !clearRemote) previous else previous.copy(
                    localCleanupPending = previous.localCleanupPending || clearLocal,
                    remoteCleanupPending = previous.remoteCleanupPending || clearRemote,
                    videoState = if (clearLocal) TaskState.CANCELLED else previous.videoState,
                    subtitleState = if (clearLocal || previous.subtitleState != TaskState.READY) TaskState.CANCELLED else previous.subtitleState,
                    videoGeneration = previous.videoGeneration + if (clearLocal) 1 else 0,
                    subtitleGeneration = previous.subtitleGeneration + 1,
                    videoError = if (clearLocal) null else previous.videoError,
                    subtitleError = null, remoteRetryRequested = false, cleanupError = null,
                    historyHidden = false, cleanupHistoryHandled = false,
                )
            } ?: return@forEach
            if (!task.cleanupPending) return@forEach
            stopForCleanup(task)
            enqueueCleanup(task, ExistingWorkPolicy.REPLACE)
        }
    }

    @Synchronized fun retryCleanup(id: String) {
        val task = store.get(id)?.takeIf { it.cleanupPending } ?: return
        store.update(id) { it.copy(cleanupError = null, historyHidden = false, cleanupHistoryHandled = false) }
        stopForCleanup(task)
        enqueueCleanup(task, ExistingWorkPolicy.REPLACE)
    }

    @Synchronized internal fun stopForCleanup(task: DownloadTask) {
        val current = store.get(task.id)?.takeIf { it.cleanupPending } ?: return
        if (current.localCleanupPending) work.cancelUniqueWork("video-${current.id}")
        work.cancelUniqueWork("subtitle-${current.id}")
        work.cancelUniqueWork("remote-cancel-${current.id}")
    }

    internal fun recover() {
        tasks.value.forEach { task ->
            if (task.cleanupPending) {
                stopForCleanup(task)
                enqueueCleanup(task, ExistingWorkPolicy.KEEP)
            }
            if (!task.localCleanupPending && !task.localFilesDeleted && task.videoState in activeStates) enqueueVideo(task, ExistingWorkPolicy.KEEP)
            if (!task.subtitleCleanupBlocked && task.subtitleState in activeStates) enqueueSubtitles(task, ExistingWorkPolicy.KEEP)
            if (!task.subtitleCleanupBlocked && task.subtitleState == TaskState.CANCELLED && task.nasBaseUrl != null && task.subtitleError != null) enqueueRemoteCancel(task)
        }
    }

    private val activeStates = setOf(TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING)
    private val network = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    private fun enqueueVideo(task: DownloadTask, policy: ExistingWorkPolicy) {
        if (task.nasMediaId != null) {
            work.enqueueUniqueWork("video-${task.id}", policy,
                OneTimeWorkRequestBuilder<NasVideoWorker>().setConstraints(network)
                    .setInputData(workDataOf("task_id" to task.id, "generation" to task.videoGeneration))
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).addTag("video").build())
            return
        }
        work.enqueueUniqueWork("video-${task.id}", policy,
            OneTimeWorkRequestBuilder<VideoWorker>().setConstraints(network)
                .setInputData(workDataOf("task_id" to task.id, "generation" to task.videoGeneration))
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).addTag("video").build())
    }

    private fun enqueueSubtitles(task: DownloadTask, policy: ExistingWorkPolicy) {
        if (task.subtitleState == TaskState.SKIPPED) return
        if (task.nasMediaId != null) {
            work.enqueueUniqueWork("subtitle-${task.id}", policy,
                OneTimeWorkRequestBuilder<NasSubtitleWorker>().setConstraints(network)
                    .setInputData(workDataOf("task_id" to task.id, "generation" to task.subtitleGeneration))
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).addTag("subtitle").build())
            return
        }
        work.enqueueUniqueWork("subtitle-${task.id}", policy,
            OneTimeWorkRequestBuilder<SubtitleWorker>().setConstraints(network)
                .setInputData(workDataOf("task_id" to task.id, "generation" to task.subtitleGeneration))
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).addTag("subtitle").build())
    }

    internal fun enqueueRemoteCancel(task: DownloadTask) {
        if (task.subtitleCleanupBlocked) return
        work.enqueueUniqueWork("remote-cancel-${task.id}", ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<RemoteCancelWorker>().setConstraints(network)
                .setInputData(workDataOf("task_id" to task.id, "remote_id" to task.remoteId))
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).build())
    }

    private fun enqueueCleanup(task: DownloadTask, policy: ExistingWorkPolicy) {
        // No network constraint: app files must be clearable while the NAS is offline.
        work.enqueueUniqueWork("cleanup-${task.id}", policy,
            OneTimeWorkRequestBuilder<CleanupWorker>()
                .setInputData(workDataOf("task_id" to task.id))
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).addTag("cleanup").build())
    }

    /** Export durable copies through SAF; app-private downloads remain usable offline. */
    suspend fun exportTask(id: String, treeUri: Uri): Int = withContext(Dispatchers.IO) {
        val task = store.get(id) ?: error("任务不存在")
        check(!task.localCleanupPending) { "正在清理本地文件，请稍后重试" }
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: error("无法打开所选目录")
        check(root.canWrite()) { "所选目录不可写，请重新选择" }
        val files = buildList {
            task.videoPath?.let { add(Triple(it, "video.${File(it).extension}", if (File(it).extension == "mp4") "video/mp4" else "video/x-matroska")) }
            task.originalPath?.let { add(Triple(it, "original.srt", "application/x-subrip")) }
            task.zhPath?.let { add(Triple(it, "zh.srt", "application/x-subrip")) }
            task.bilingualPath?.let { add(Triple(it, "bilingual.srt", "application/x-subrip")) }
        }.filter { File(it.first).isFile }
        check(files.isNotEmpty()) { "还没有下载完成的文件" }
        val safeTitle = task.title.replace(Regex("[\\\\/:*?\"<>|\\p{Cntrl}]"), "_").take(64).ifBlank { "视频" }
        val baseName = "$safeTitle-${task.id.take(8)}"
        var name = baseName
        var suffix = 1
        while (root.findFile(name) != null) { name = "$baseName-${suffix++}" }
        val folder = root.createDirectory(name) ?: error("无法创建保存目录")
        try {
            for ((path, filename, mime) in files) {
                val target = folder.createFile(mime, filename) ?: error("无法创建 $filename")
                val output = context.contentResolver.openOutputStream(target.uri, "w") ?: error("无法写入 $filename")
                output.use { dest -> File(path).inputStream().use { it.copyTo(dest) } }
            }
        } catch (error: Throwable) {
            folder.delete() // Only remove the new partial export, never pre-existing user content.
            throw error
        }
        files.size
    }

    /** No URLs, titles, subtitle contents, tokens, proxy credentials, or local paths. */
    fun diagnostics(): String = JSONObject().apply {
        put("app", "Qwen Subtitle Android"); put("api_version", 1)
        put("downloader_version", BundledDownloader.version(BundledDownloader.coreFile(context)) ?: "尚未初始化")
        put("android_api", android.os.Build.VERSION.SDK_INT)
        put("generated_at", System.currentTimeMillis())
        put("tasks", JSONArray().apply {
            tasks.value.forEach { task -> put(JSONObject().apply {
                put("id", task.id); put("remote_id", task.remoteId)
                put("video_state", task.videoState); put("subtitle_state", task.subtitleState)
                put("video_progress", task.videoProgress.toDouble()); put("subtitle_progress", task.subtitleProgress.toDouble())
                put("video_error", task.videoError); put("subtitle_error", task.subtitleError)
                put("local_cleanup_pending", task.localCleanupPending); put("remote_cleanup_pending", task.remoteCleanupPending)
                put("local_files_deleted", task.localFilesDeleted); put("remote_files_deleted", task.remoteFilesDeleted)
                put("cleanup_error", task.cleanupError)
                put("history_hidden", task.historyHidden)
                put("created_at", task.createdAt); put("updated_at", task.updatedAt)
                put("video_available", task.videoPath?.let { File(it).isFile } ?: false)
                put("bilingual_available", task.bilingualPath?.let { File(it).isFile } ?: false)
            }) }
        })
    }.toString(2)
}

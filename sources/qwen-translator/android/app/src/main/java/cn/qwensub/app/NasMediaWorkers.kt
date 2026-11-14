package cn.qwensub.app

import android.content.Context
import android.util.AtomicFile
import androidx.work.WorkerParameters
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import java.io.File

/** Uses the existing independent video/subtitle task controls for NAS offline copies. */
class NasVideoWorker(context: Context, parameters: WorkerParameters) : MediaWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val task = current(false) ?: return Result.success()
        if (task.videoState == TaskState.READY && task.videoPath?.let { File(it).isFile } == true) return Result.success()
        return try {
            foreground("正在保存 NAS 视频到手机", false)
            val settings = repo.settings()
            if (settings.baseUrl != task.nasBaseUrl) throw NasException("请恢复此任务原来的飞牛地址后继续下载", false)
            val client = MediaClient(settings)
            val detail = client.file(requireNotNull(task.nasMediaId))
            val extension = (if (task.nasStreamPath?.endsWith("/compatible/stream") == true) "mkv" else detail.filename.substringAfterLast('.', "mkv").lowercase())
                .takeIf { it in setOf("mp4", "mkv", "m4v", "webm", "avi", "ts", "mov") } ?: "mkv"
            update(false) { it.copy(videoState = TaskState.RUNNING, videoError = null) }
            val file = File(applicationContext.filesDir, "media/$taskId/video/video.$extension")
            var lastUpdate = 0L
            runDownload("video-$taskId-$generation") {
                MediaFiles.locked(taskId) {
                    if (current(false) == null) throw CancellationException("视频任务已停止")
                    runBlocking {
                        client.download(task.nasStreamPath ?: detail.streamPath, file) { progress ->
                            if (current(false) == null || Thread.currentThread().isInterrupted) throw CancellationException("视频任务已停止")
                            val now = System.currentTimeMillis()
                            if (now - lastUpdate >= 1000 || progress >= 1f) {
                                update(false) { it.copy(videoProgress = progress) }; lastUpdate = now
                            }
                        }
                    }
                }
            }
            currentCoroutineContext().ensureActive()
            val completed = update(false) { it.copy(videoState = TaskState.READY, videoProgress = 1f,
                videoPath = file.absolutePath, videoError = null) }
            if (completed != null) repo.store.finalizeCleanup(taskId, generation, completed.subtitleGeneration)
            Result.success()
        } catch (error: CancellationException) {
            update(false) { it.copy(videoState = TaskState.WAITING, videoError = "下载暂停，恢复连接后继续") }
            throw error
        } catch (error: Exception) {
            val retry = shouldRetry(error)
            update(false) { it.copy(videoState = if (retry) TaskState.WAITING else TaskState.FAILED, videoError = safeError(error)) }
            if (retry) Result.retry() else Result.failure()
        }
    }
}

class NasSubtitleWorker(context: Context, parameters: WorkerParameters) : MediaWorker(context, parameters) {
    override suspend fun doWork(): Result {
        var task = current(true) ?: return Result.success()
        if (task.subtitleState in setOf(TaskState.READY, TaskState.SKIPPED)) return Result.success()
        return try {
            foreground("正在准备 NAS 字幕", true)
            val settings = repo.settings()
            if (settings.baseUrl != task.nasBaseUrl) throw NasException("请恢复此任务原来的飞牛地址后继续准备字幕", false)
            val client = MediaClient(settings)
            if (task.remoteId == null && client.file(requireNotNull(task.nasMediaId)).subtitles.isEmpty()) {
                update(true) { it.copy(subtitleState = TaskState.SKIPPED,
                    subtitleError = "未找到可用字幕，已跳过字幕处理；视频仍可观看。", subtitleProgress = 0f) }
                return Result.success()
            }
            update(true) { it.copy(subtitleState = TaskState.RUNNING, subtitleError = null) }
            // Preserve a usable original while translation proceeds independently of the video.
            if (task.originalPath?.let { File(it).isFile } != true && task.nasOriginalPath != null) {
                val text = client.text(task.nasOriginalPath)
                save("original", text)
            }
            if (task.remoteId == null) {
                if (task.nasOriginalPath == null) {
                    val detail = client.file(requireNotNull(task.nasMediaId))
                    val source = detail.subtitles.firstOrNull { it.id == task.nasSubtitleId } ?: detail.subtitles.firstOrNull()
                    if (source != null) save("original", client.text(source.downloadPath))
                }
                task.nasZhPath?.let { save("zh", client.text(it)) }
                task.nasBilingualPath?.let { save("bilingual", client.text(it)) }
                update(true) { it.copy(subtitleState = TaskState.READY, subtitleProgress = 1f, subtitleError = null) }
                return Result.success()
            }
            task = current(true) ?: return Result.success()
            val remoteId = requireNotNull(task.remoteId)
            val deadline = System.currentTimeMillis() + 6 * 60 * 1000
            while (System.currentTimeMillis() < deadline) {
                currentCoroutineContext().ensureActive()
                task = current(true) ?: return Result.success()
                var job = client.job(remoteId)
                if (job.state in setOf("failed", "cancelled") && task.remoteRetryRequested) {
                    job = client.retry(remoteId)
                    update(true) { it.copy(remoteRetryRequested = false) }
                }
                when (job.state) {
                    "ready" -> {
                        for (kind in listOf("original", "zh", "bilingual")) save(kind, client.jobSubtitle(remoteId, kind))
                        update(true) { it.copy(subtitleState = TaskState.READY, subtitleProgress = 1f,
                            subtitleError = null, remoteRetryRequested = false) }
                        return Result.success()
                    }
                    "failed" -> throw NasException(job.message.ifBlank { "字幕准备失败，可重新尝试" }, false)
                    "cancelled" -> {
                        update(true) { it.copy(subtitleState = TaskState.CANCELLED, subtitleError = "飞牛字幕任务已停止") }
                        return Result.success()
                    }
                    "queued", "running" -> update(true) { it.copy(subtitleState = TaskState.RUNNING,
                        subtitleProgress = job.progress.coerceIn(0f, 0.95f), subtitleError = null) }
                    else -> throw NasException("字幕任务状态不兼容，请更新飞牛服务", false)
                }
                delay(5000)
            }
            update(true) { it.copy(subtitleState = TaskState.WAITING, subtitleError = "飞牛仍在准备字幕，稍后继续同步") }
            Result.retry()
        } catch (error: CancellationException) {
            update(true) { it.copy(subtitleState = TaskState.WAITING, subtitleError = "连接恢复后继续同步字幕") }
            throw error
        } catch (error: Exception) {
            val retry = shouldRetry(error)
            update(true) { it.copy(subtitleState = if (retry) TaskState.WAITING else TaskState.FAILED, subtitleError = safeError(error)) }
            if (retry) Result.retry() else Result.failure()
        }
    }

    private suspend fun save(kind: String, text: String) = withContext(Dispatchers.IO) {
        require(text.contains("-->")) { "飞牛返回的字幕内容无效" }
        currentCoroutineContext().ensureActive()
        MediaFiles.locked(taskId) {
            if (current(true) == null) return@locked
            val file = AtomicFile(File(applicationContext.filesDir, "media/$taskId/subtitles/$kind.srt").also { it.parentFile?.mkdirs() })
            val stream = file.startWrite()
            try { stream.write(text.toByteArray(Charsets.UTF_8)); file.finishWrite(stream) }
            catch (error: Throwable) { file.failWrite(stream); throw error }
            update(true) {
                when (kind) {
                    "original" -> it.copy(originalPath = file.baseFile.absolutePath)
                    "zh" -> it.copy(zhPath = file.baseFile.absolutePath)
                    else -> it.copy(bilingualPath = file.baseFile.absolutePath)
                }
            }
        }
    }
}

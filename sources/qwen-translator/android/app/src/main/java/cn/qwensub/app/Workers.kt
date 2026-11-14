package cn.qwensub.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.AtomicFile
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException

abstract class MediaWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    internal val repo by lazy { AppRepository.get(applicationContext) }
    protected val taskId = inputData.getString("task_id").orEmpty()
    protected val generation = inputData.getInt("generation", 0)

    protected suspend fun foreground(label: String, subtitle: Boolean) {
        val manager = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(NotificationChannel("downloads", "下载与字幕任务", NotificationManager.IMPORTANCE_LOW))
        val intent = applicationContext.packageManager.getLaunchIntentForPackage(applicationContext.packageName)
        val notification = NotificationCompat.Builder(applicationContext, "downloads")
            .setSmallIcon(android.R.drawable.stat_sys_download).setContentTitle(label)
            .setContentText("视频和字幕独立处理，可在应用内分别停止")
            .setOnlyAlertOnce(true).setOngoing(true).setProgress(0, 0, true).apply {
                if (intent != null) setContentIntent(PendingIntent.getActivity(applicationContext, 0, intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
            }.build()
        val id = ("${if (subtitle) "subtitle" else "video"}-$taskId".hashCode() and 0x7fffffff).coerceAtLeast(1)
        setForeground(if (Build.VERSION.SDK_INT >= 29) ForegroundInfo(id, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            else ForegroundInfo(id, notification))
    }

    protected fun current(subtitle: Boolean): DownloadTask? = repo.store.get(taskId)?.takeIf {
        if (subtitle) !it.subtitleCleanupBlocked && it.subtitleGeneration == generation && it.subtitleState != TaskState.CANCELLED
        else !it.localCleanupPending && !it.localFilesDeleted && it.videoGeneration == generation && it.videoState != TaskState.CANCELLED
    }

    protected fun update(subtitle: Boolean, transform: (DownloadTask) -> DownloadTask): DownloadTask? = repo.store.update(taskId) {
        if (subtitle && !it.subtitleCleanupBlocked && it.subtitleGeneration == generation && it.subtitleState != TaskState.CANCELLED ||
            !subtitle && !it.localCleanupPending && !it.localFilesDeleted && it.videoGeneration == generation && it.videoState != TaskState.CANCELLED) transform(it) else it
    }

    protected fun shouldRetry(error: Throwable): Boolean = when (error) {
        is NasException -> error.retryable
        is IOException -> true
        is YoutubeDLException -> {
            val message = error.message.orEmpty().lowercase()
            listOf("timed out", "timeout", "network", "connection", "resolve", "unreachable", "http error 429", "http error 5", "temporary failure", "remote end closed").any { it in message }
        }
        else -> false
    }
}

class VideoWorker(context: Context, parameters: WorkerParameters) : MediaWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val task = current(false) ?: return Result.success()
        if (task.videoState == TaskState.READY && task.videoPath?.let { File(it).isFile } == true) {
            repo.store.finalizeCleanup(taskId, task.videoGeneration, task.subtitleGeneration)
            return Result.success()
        }
        try {
            foreground("正在下载视频", false)
            update(false) { it.copy(videoState = TaskState.RUNNING, videoError = null) }
            var lastProgress = -1f
            var lastUpdate = 0L
            val (file, title) = runDownload("video-$taskId-$generation") {
                MediaFiles.locked(taskId) {
                    val active = current(false) ?: throw CancellationException("视频任务已停止")
                    DownloadEngine(applicationContext).video(active, repo.settings().downloadProxy) { progress ->
                        val now = System.currentTimeMillis()
                        if (now - lastUpdate > 1000 || progress - lastProgress >= 0.02f) {
                            update(false) { it.copy(videoProgress = progress) }
                            lastUpdate = now; lastProgress = progress
                        }
                    }
                }
            }
            currentCoroutineContext().ensureActive()
            val completed = update(false) { it.copy(videoState = TaskState.READY, videoProgress = 1f,
                videoPath = file.absolutePath, title = title ?: it.title, videoError = null) }
            if (completed != null) repo.store.finalizeCleanup(taskId, generation, completed.subtitleGeneration)
            return Result.success()
        } catch (error: CancellationException) {
            // WorkManager will rerun after a constraint/OS stop; an explicit user stop changed generation.
            update(false) { it.copy(videoState = TaskState.WAITING, videoError = "任务暂停，网络恢复后继续下载") }
            throw error
        } catch (error: YoutubeDL.CanceledException) {
            return if (current(false) == null) Result.success() else Result.retry()
        } catch (error: Exception) {
            val retry = shouldRetry(error)
            update(false) { it.copy(videoState = if (retry) TaskState.WAITING else TaskState.FAILED, videoError = safeError(error)) }
            return if (retry) Result.retry() else Result.failure()
        } finally {
            DownloadEngine.stop("video-$taskId-$generation")
        }
    }
}

class SubtitleWorker(context: Context, parameters: WorkerParameters) : MediaWorker(context, parameters) {
    override suspend fun doWork(): Result {
        var task = current(true) ?: return Result.success()
        if (task.subtitleState == TaskState.READY) return Result.success()
        try {
            foreground("正在准备字幕", true)
            update(true) { it.copy(subtitleState = TaskState.RUNNING, subtitleError = null) }
            if (task.originalPath?.let { File(it).isFile } != true) {
                val (original, title) = runDownload("subtitle-$taskId-$generation") {
                    MediaFiles.locked(taskId) {
                        val active = current(true) ?: throw CancellationException("字幕任务已停止")
                        DownloadEngine(applicationContext).subtitles(active, repo.settings().downloadProxy)
                    }
                }
                currentCoroutineContext().ensureActive()
                update(true) { it.copy(originalPath = original.absolutePath, title = title ?: it.title, subtitleProgress = 0.1f) }
            }
            task = current(true) ?: return Result.success()
            val settings = repo.settings()
            if (settings.baseUrl.isBlank() || settings.token.isBlank()) {
                update(true) { it.copy(subtitleState = TaskState.WAITING, subtitleError = "原字幕已保存，请填写飞牛地址和访问令牌") }
                return Result.retry()
            }
            if (task.nasBaseUrl != null && task.nasBaseUrl != settings.baseUrl) {
                throw NasException("此任务属于先前的飞牛地址，请恢复原连接设置后重试", false)
            }
            val client = NasClient(settings)
            if (task.remoteId == null) {
                update(true) { it.copy(nasBaseUrl = settings.baseUrl, submissionTitle = it.submissionTitle ?: it.title.take(500)) }
                task = current(true) ?: return Result.success()
                // Capture and persist a remote ID even when cancellation arrives during the POST.
                // The same UUID lets another attempt recover a reply lost during process death.
                val submitted = withContext(Dispatchers.IO + NonCancellable) {
                    val original = MediaFiles.locked(taskId) {
                        if (current(true) == null) throw CancellationException("字幕任务已停止")
                        File(requireNotNull(task.originalPath)).readText(Charsets.UTF_8)
                    }
                    if (current(true) == null) throw CancellationException("字幕任务已停止")
                    client.submit(task.copy(title = requireNotNull(task.submissionTitle)), original).also { remote ->
                        val saved = repo.store.update(taskId) {
                            // Local-only cleanup keeps the NAS identity, but never resurrect a deleted remote request.
                            if (it.clientRequestId == task.clientRequestId && !it.remoteCleanupPending && !it.remoteFilesDeleted) it.copy(remoteId = remote.id) else it
                        }
                        if (saved?.subtitleState == TaskState.CANCELLED && !saved.subtitleCleanupBlocked) repo.enqueueRemoteCancel(saved)
                    }
                }
                update(true) { it.copy(remoteId = submitted.id, subtitleProgress = 0.15f) }
            }
            task = current(true) ?: return Result.success()
            val remoteId = requireNotNull(task.remoteId)
            // Stay under WorkManager's ordinary ten-minute execution window. Persist between polls.
            val deadline = System.currentTimeMillis() + 6 * 60 * 1000
            while (System.currentTimeMillis() < deadline) {
                currentCoroutineContext().ensureActive()
                task = current(true) ?: return Result.success()
                var remote = client.status(remoteId)
                if (current(true) == null) return Result.success()
                if (remote.state in setOf("failed", "cancelled") && task.remoteRetryRequested) {
                    remote = client.retry(remoteId)
                    update(true) { it.copy(remoteRetryRequested = false) }
                }
                when (remote.state) {
                    "ready" -> {
                        val originalText = client.subtitle(remoteId, "original")
                        val chineseText = client.subtitle(remoteId, "zh")
                        val bilingualText = client.subtitle(remoteId, "bilingual")
                        currentCoroutineContext().ensureActive()
                        MediaFiles.locked(taskId) {
                            if (current(true) == null) return@locked
                            val directory = File(applicationContext.filesDir, "media/$taskId/subtitles").apply { mkdirs() }
                            val original = writeSubtitle(directory, "original.srt", originalText)
                            val chinese = writeSubtitle(directory, "zh.srt", chineseText)
                            val bilingual = writeSubtitle(directory, "bilingual.srt", bilingualText)
                            update(true) { it.copy(subtitleState = TaskState.READY, subtitleProgress = 1f,
                                originalPath = original.absolutePath, zhPath = chinese.absolutePath,
                                bilingualPath = bilingual.absolutePath, subtitleError = null, remoteRetryRequested = false) }
                        }
                        return Result.success()
                    }
                    "failed" -> throw NasException(remote.message.ifBlank { "飞牛翻译失败，请重试字幕任务" }, false)
                    "cancelled" -> {
                        update(true) { it.copy(subtitleState = TaskState.CANCELLED, subtitleError = "飞牛任务已取消") }
                        return Result.success()
                    }
                    "queued", "running" -> update(true) { it.copy(subtitleState = TaskState.RUNNING,
                        subtitleProgress = (0.15f + remote.progress * 0.8f).coerceIn(0.15f, 0.95f), subtitleError = null) }
                    else -> throw NasException("飞牛返回了未知任务状态，请更新应用与服务", false)
                }
                delay(5000)
            }
            update(true) { it.copy(subtitleState = TaskState.WAITING, subtitleError = "飞牛仍在翻译，稍后自动获取结果") }
            return Result.retry()
        } catch (error: CancellationException) {
            update(true) { it.copy(subtitleState = TaskState.WAITING, subtitleError = "连接恢复后自动获取字幕；飞牛翻译继续运行") }
            throw error
        } catch (error: YoutubeDL.CanceledException) {
            return if (current(true) == null) Result.success() else Result.retry()
        } catch (error: Exception) {
            val retry = shouldRetry(error)
            update(true) { it.copy(subtitleState = if (retry) TaskState.WAITING else TaskState.FAILED, subtitleError = safeError(error)) }
            return if (retry) Result.retry() else Result.failure()
        } finally {
            DownloadEngine.stop("subtitle-$taskId-$generation")
        }
    }

    private fun writeSubtitle(directory: File, name: String, contents: String): File {
        require(contents.contains("-->")) { "飞牛返回的字幕格式无效" }
        val file = AtomicFile(File(directory, name))
        val stream = file.startWrite()
        try {
            stream.write(contents.toByteArray(Charsets.UTF_8)); file.finishWrite(stream)
        } catch (error: Throwable) {
            file.failWrite(stream); throw error
        }
        return file.baseFile
    }
}

class RemoteCancelWorker(context: Context, parameters: WorkerParameters) : MediaWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val task = repo.store.get(taskId) ?: return Result.success()
        if (task.subtitleState != TaskState.CANCELLED || task.subtitleCleanupBlocked) return Result.success()
        return try {
            val settings = repo.settings()
            if (settings.baseUrl != task.nasBaseUrl) return Result.retry()
            if (task.nasMediaId != null) {
                val client = MediaClient(settings)
                // Offline copies of original NAS captions never create a translation job.
                val remote = task.remoteId ?: run {
                    repo.store.update(taskId) { if (it.subtitleState == TaskState.CANCELLED) it.copy(subtitleError = null) else it }
                    return Result.success()
                }
                val latest = repo.store.get(taskId)
                if (latest?.subtitleState == TaskState.CANCELLED && !latest.subtitleCleanupBlocked && latest.clientRequestId == task.clientRequestId) {
                    client.cancel(remote)
                    repo.store.update(taskId) { if (it.subtitleState == TaskState.CANCELLED && !it.subtitleCleanupBlocked) it.copy(subtitleError = null) else it }
                }
                return Result.success()
            }
            val client = NasClient(settings)
            // Recover a successful POST whose reply was lost before local cancellation.
            val remoteId = task.remoteId ?: withContext(Dispatchers.IO + NonCancellable) {
                val original = task.originalPath ?: return@withContext null
                val title = task.submissionTitle ?: return@withContext null
                val text = MediaFiles.locked(taskId) {
                    val current = repo.store.get(taskId)
                    if (current?.subtitleCleanupBlocked != false || current.clientRequestId != task.clientRequestId ||
                        current.subtitleState != TaskState.CANCELLED) return@locked null
                    File(original).readText(Charsets.UTF_8)
                } ?: return@withContext null
                if (repo.store.get(taskId)?.subtitleCleanupBlocked != false) return@withContext null
                client.submit(task.copy(title = title), text).also { remote ->
                    repo.store.update(taskId) {
                        if (!it.remoteCleanupPending && !it.remoteFilesDeleted && it.clientRequestId == task.clientRequestId) it.copy(remoteId = remote.id) else it
                    }
                }.id
            } ?: return Result.success()
            val current = repo.store.get(taskId) ?: return Result.success()
            if (current.subtitleState != TaskState.CANCELLED || current.subtitleCleanupBlocked || current.clientRequestId != task.clientRequestId) return Result.success()
            client.cancel(remoteId)
            repo.store.update(taskId) { if (it.subtitleState == TaskState.CANCELLED && !it.subtitleCleanupBlocked && it.clientRequestId == task.clientRequestId) it.copy(subtitleError = null) else it }
            Result.success()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            repo.store.update(taskId) {
                if (it.subtitleState == TaskState.CANCELLED && !it.subtitleCleanupBlocked && it.clientRequestId == task.clientRequestId) it.copy(subtitleError = "等待向飞牛同步取消：${safeError(error)}") else it
            }
            if (shouldRetry(error)) Result.retry() else Result.failure()
        }
    }
}

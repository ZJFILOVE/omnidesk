package cn.qwensub.app

import android.content.Context
import androidx.work.WorkerParameters
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Durable deletion outbox. A failed NAS operation never restores files already cleared locally. */
class CleanupWorker(context: Context, parameters: WorkerParameters) : MediaWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val initial = repo.store.get(taskId)?.takeIf { it.cleanupPending } ?: return Result.success()
        repo.stopForCleanup(initial)
        val errors = mutableListOf<Pair<String, Throwable>>()
        try {
            withContext(Dispatchers.IO) {
                DownloadRuns.stopAndJoin(taskId, video = initial.localCleanupPending, subtitle = true,
                    videoBefore = initial.videoGeneration, subtitleBefore = initial.subtitleGeneration)
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            repo.store.update(taskId) {
                if (it.cleanupPending && sameCleanup(it, initial))
                    it.copy(cleanupError = safeError(error)) else it
            }
            return Result.retry()
        }

        if (repo.store.get(taskId)?.let { it.localCleanupPending && sameCleanup(it, initial) } == true) {
            try {
                withContext(Dispatchers.IO) {
                    MediaFiles.exclusive(taskId) {
                        // REPLACE can briefly overlap cancelled IO: recheck after taking the deletion lock.
                        if (repo.store.get(taskId)?.let { it.localCleanupPending && sameCleanup(it, initial) } != true) return@exclusive
                        MediaFiles.deleteTask(applicationContext, taskId)
                        repo.store.update(taskId) {
                            if (!it.localCleanupPending || !sameCleanup(it, initial)) it else it.copy(
                                localCleanupPending = false, localFilesDeleted = true,
                                videoPath = null, originalPath = null, zhPath = null, bilingualPath = null,
                                videoState = TaskState.CANCELLED, subtitleState = TaskState.CANCELLED,
                                videoProgress = 0f, subtitleProgress = 0f, videoError = null, subtitleError = null,
                            )
                        }
                    }
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                errors += "手机清理失败" to error
            }
        }

        val remoteTask = repo.store.get(taskId)?.takeIf { it.remoteCleanupPending && sameCleanup(it, initial) }
        if (remoteTask != null) {
            try {
                if (remoteTask.nasBaseUrl != null && !(remoteTask.nasMediaId != null && remoteTask.remoteId == null)) {
                    val settings = repo.settings()
                    if (settings.baseUrl != remoteTask.nasBaseUrl) {
                        throw NasException("此任务属于先前的飞牛地址，请恢复原连接设置后重试清理", false)
                    }
                    if (remoteTask.nasMediaId != null) {
                        MediaClient(settings).deleteRequest(remoteTask.clientRequestId)
                        MediaRequestStore(applicationContext).forget(remoteTask.clientRequestId)
                    }
                    else NasClient(settings).deleteRequest(remoteTask.clientRequestId)
                } else if (remoteTask.remoteId != null) {
                    throw NasException("此任务缺少原飞牛地址，无法确认清理目标", false)
                }
                repo.store.update(taskId) {
                    if (!it.remoteCleanupPending || !sameCleanup(it, initial)) it else it.copy(
                        remoteCleanupPending = false, remoteFilesDeleted = true,
                        remoteRetryRequested = false, remoteId = null, subtitleError = null,
                    )
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                errors += "飞牛清理失败" to error
            }
        }

        repo.store.update(taskId) {
            if (!sameCleanup(it, initial)) it
            else it.copy(cleanupError = errors.takeIf { values -> values.isNotEmpty() && it.cleanupPending }
                ?.joinToString("；") { (label, error) -> "$label：${safeError(error)}" })
        }
        repo.store.finalizeCleanup(taskId, initial.videoGeneration, initial.subtitleGeneration)
        return when {
            errors.isEmpty() -> Result.success()
            errors.any { shouldRetry(it.second) } -> Result.retry()
            else -> Result.failure()
        }
    }

    private fun sameCleanup(current: DownloadTask, initial: DownloadTask): Boolean =
        current.videoGeneration == initial.videoGeneration &&
            current.subtitleGeneration == initial.subtitleGeneration && current.clientRequestId == initial.clientRequestId
}

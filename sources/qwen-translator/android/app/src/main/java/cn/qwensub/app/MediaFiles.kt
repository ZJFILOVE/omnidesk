package cn.qwensub.app

import android.content.Context
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.locks.ReentrantReadWriteLock
import kotlin.concurrent.read
import kotlin.concurrent.write

/** Serialize each task's filesystem writes and deletion; never follow user-supplied saved paths. */
internal object MediaFiles {
    private val locks = ConcurrentHashMap<String, ReentrantReadWriteLock>()
    private fun lock(taskId: String) = locks.getOrPut(validId(taskId)) { ReentrantReadWriteLock(true) }

    // Video and subtitle downloads can run concurrently; deletion waits for all admitted writers.
    fun <T> locked(taskId: String, block: () -> T): T = lock(taskId).read(block)
    fun <T> exclusive(taskId: String, block: () -> T): T = lock(taskId).write(block)

    fun deleteTask(context: Context, taskId: String) = exclusive(taskId) {
        val media = File(context.filesDir, "media").toPath()
        val cache = File(context.cacheDir, "yt-cache").toPath()
        check(!Files.isSymbolicLink(media) && !Files.isSymbolicLink(cache)) { "应用缓存目录异常，未清理文件" }
        deleteTree(media.resolve(taskId))
        if (Files.exists(cache, LinkOption.NOFOLLOW_LINKS)) {
            val ownedCache = Regex("(?:video|subtitle)-${Regex.escape(taskId)}-\\d+")
            Files.newDirectoryStream(cache).use { children ->
                children.filter { ownedCache.matches(it.fileName.toString()) }.forEach(::deleteTree)
            }
        }
    }

    private fun validId(id: String): String {
        require(Regex("[A-Za-z0-9_-]{1,128}").matches(id)) { "任务编号无效，未清理文件" }
        return id
    }

    private fun deleteTree(path: Path) {
        if (Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) {
            Files.newDirectoryStream(path).use { children -> children.forEach(::deleteTree) }
        }
        Files.deleteIfExists(path)
    }
}

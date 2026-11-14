package cn.qwensub.app

import android.content.Context
import android.util.AtomicFile
import android.system.Os
import android.system.OsConstants
import com.yausername.ffmpeg.FFmpeg
import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLRequest
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.TimeUnit
import java.io.IOException
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

private val downloaderThreads = Executors.newCachedThreadPool { action ->
    Thread(action, "media-downloader").apply { isDaemon = true }
}

/** Track the actual reader thread, not Future cancellation, which can complete before it exits. */
internal object DownloadRuns {
    internal class Run(val processId: String) {
        val finished = CountDownLatch(1)
        @Volatile var thread: Thread? = null
        @Volatile var cancelled = false
        fun cancel() {
            cancelled = true
            DownloadEngine.stop(processId)
            thread?.interrupt()
        }
    }
    private val active = ConcurrentHashMap<String, CopyOnWriteArraySet<Run>>()
    fun register(processId: String): Run = Run(processId).also { active.getOrPut(processId) { CopyOnWriteArraySet() }.add(it) }
    fun finish(run: Run) {
        run.finished.countDown()
        active[run.processId]?.remove(run)
    }
    fun stopAndJoin(taskId: String, video: Boolean, subtitle: Boolean,
                    videoBefore: Int = Int.MAX_VALUE, subtitleBefore: Int = Int.MAX_VALUE) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30)
        val videoId = Regex("video-${Regex.escape(taskId)}-\\d+")
        val subtitleId = Regex("subtitle-${Regex.escape(taskId)}-\\d+")
        while (true) {
            val runs = active.entries.filter {
                val generation = it.key.substringAfterLast('-').toIntOrNull() ?: Int.MAX_VALUE
                video && videoId.matches(it.key) && generation < videoBefore ||
                    subtitle && subtitleId.matches(it.key) && generation < subtitleBefore
            }.flatMap { it.value.toList() }
            if (runs.isEmpty()) return
            runs.forEach { it.cancel() }
            runs.forEach { it.finished.await(100, TimeUnit.MILLISECONDS) }
            if (System.nanoTime() >= deadline) throw IOException("下载任务仍在停止，稍后自动重试清理")
        }
    }
}

/** Destroy the registered native process before interrupting its Java reader thread. */
internal suspend fun <T> runDownload(processId: String, block: () -> T): T = suspendCancellableCoroutine { continuation ->
    val run = DownloadRuns.register(processId)
    continuation.invokeOnCancellation { run.cancel() }
    downloaderThreads.execute {
        run.thread = Thread.currentThread()
        try {
            if (run.cancelled) throw InterruptedException()
            val result = block()
            if (continuation.isActive) continuation.resume(result)
        } catch (error: Throwable) {
            if (continuation.isActive) continuation.resumeWithException(error)
        } finally {
            DownloadEngine.stop(processId)
            DownloadEngine.release(processId)
            DownloadRuns.finish(run)
        }
    }
}

internal class DownloadEngine(private val context: Context) {
    companion object {
        private val initLock = Any()
        private val stopLock = Any()
        @Volatile private var initialized = false
        private data class Marker(val cache: String, val python: String, val script: String)
        private data class OwnedProcess(val pid: Int, val parent: Int, val started: String, val args: List<String>)
        private val processMarkers = ConcurrentHashMap<String, Marker>()

        fun stop(processId: String) = synchronized(stopLock) {
            processMarkers[processId]?.let { terminateOwnedTree(it) }
            // Keep the wrapper's registry consistent even if its process has already been killed.
            YoutubeDL.destroyProcessById(processId)
        }

        fun release(processId: String) { processMarkers.remove(processId) }

        private fun started(pid: Int): String? = runCatching {
            File("/proc/$pid/stat").readText().substringAfterLast(") ").split(' ').getOrNull(19)
        }.getOrNull()

        /** Inspect only processes belonging to this app; never read other users' command lines. */
        private fun ownedProcesses(): List<OwnedProcess> = File("/proc").listFiles().orEmpty().mapNotNull { directory ->
            val pid = directory.name.toIntOrNull() ?: return@mapNotNull null
            runCatching {
                if (Os.stat(directory.path).st_uid != android.os.Process.myUid()) return@runCatching null
                val status = File(directory, "status").readText()
                val parent = Regex("(?m)^PPid:\\s+(\\d+)").find(status)?.groupValues?.get(1)?.toIntOrNull()
                    ?: return@runCatching null
                val identity = started(pid) ?: return@runCatching null
                val args = File(directory, "cmdline").readText().split('\u0000').filter { it.isNotEmpty() }
                OwnedProcess(pid, parent, identity, args)
            }.getOrNull()
        }

        private fun signal(process: OwnedProcess, signal: Int): Boolean = runCatching {
            if (Os.stat("/proc/${process.pid}").st_uid != android.os.Process.myUid() || started(process.pid) != process.started) return@runCatching false
            Os.kill(process.pid, signal)
            true
        }.getOrDefault(false)

        private fun terminateOwnedTree(marker: Marker) {
            val processes = ownedProcesses()
            val roots = processes.filter { process ->
                val cacheIndex = process.args.indexOf("--cache-dir")
                process.parent == android.os.Process.myPid() && process.args.getOrNull(0) == marker.python &&
                    process.args.getOrNull(1) == marker.script && cacheIndex >= 0 && process.args.getOrNull(cacheIndex + 1) == marker.cache
            }
            if (roots.isEmpty()) return
            val frozen = linkedMapOf<Int, OwnedProcess>()
            try {
                roots.forEach { if (signal(it, OsConstants.SIGSTOP)) frozen[it.pid] = it }
                // Freezing each parent prevents further children. Repeat snapshots to include a child
                // created just before its parent stopped; every included PID must descend from a root.
                repeat(4) {
                    val snapshot = ownedProcesses()
                    var changed: Boolean
                    do {
                        changed = false
                        snapshot.filter { it.pid !in frozen && it.parent in frozen }.forEach { child ->
                            if (signal(child, OsConstants.SIGSTOP)) { frozen[child.pid] = child; changed = true }
                        }
                    } while (changed && frozen.size < 512)
                }
            } finally {
                // Insertion order is parent-first; terminate the descendants before their parents.
                frozen.values.toList().asReversed().forEach { process ->
                    if (!signal(process, OsConstants.SIGKILL)) signal(process, OsConstants.SIGCONT)
                }
            }
        }
    }

    private fun initialize() = synchronized(initLock) {
        if (!initialized) {
            YoutubeDL.init(context)
            FFmpeg.init(context)
            check(File(context.applicationInfo.nativeLibraryDir, "libqjs.so").exists()) { "安装包缺少 QuickJS，请安装完整 APK" }
            BundledDownloader.ensure(context)
            initialized = true
        }
    }

    private fun request(url: String, proxy: String, processId: String) = YoutubeDLRequest(InputValidation.youtubeUrl(url)).apply {
        addOption("--no-playlist"); addOption("--newline")
        addOption("--socket-timeout", 30); addOption("--retries", 3); addOption("--fragment-retries", 3)
        addOption("--continue"); addOption("--no-overwrites"); addOption("--no-mtime")
        addOption("--match-filter", "!is_live")
        val cache = File(context.cacheDir, "yt-cache/$processId").absolutePath
        addOption("--cache-dir", cache)
        processMarkers[processId] = Marker(cache, File(context.applicationInfo.nativeLibraryDir, "libpython.so").absolutePath,
            File(context.noBackupFilesDir, "youtubedl-android/yt-dlp/yt-dlp").absolutePath)
        if (proxy.isNotBlank()) addOption("--proxy", proxy)
    }

    fun video(task: DownloadTask, proxy: String, progress: (Float) -> Unit): Pair<File, String?> {
        initialize()
        if (Thread.currentThread().isInterrupted) throw InterruptedException()
        val directory = File(context.filesDir, "media/${task.id}/video").apply { mkdirs() }
        val request = request(task.url, proxy, "video-${task.id}-${task.videoGeneration}").apply {
            // AVC/AAC MP4 plays on older Android devices too; cap at 1080p for mobile storage.
            addOption("-f", "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4][height<=1080]/b[height<=1080]")
            addOption("--check-formats")
            // Some media URLs accept small range probes but reject large or open-ended GETs.
            addOption("--http-chunk-size", 1024 * 1024)
            addOption("--merge-output-format", "mp4"); addOption("--write-info-json")
            addOption("-o", File(directory, "video.%(ext)s").absolutePath)
        }
        YoutubeDL.execute(request, "video-${task.id}-${task.videoGeneration}", callback = { percent, _, _ ->
            if (percent.isFinite() && percent >= 0) progress((percent / 100).coerceIn(0f, 0.99f))
        })
        val file = directory.listFiles().orEmpty().firstOrNull {
            it.extension.lowercase() in setOf("mp4", "mkv", "webm", "mov") && it.length() > 0 && !it.name.contains(".f")
        } ?: error("下载完成但没有找到视频文件")
        return file to title(directory)
    }

    fun subtitles(task: DownloadTask, proxy: String): Pair<File, String?> {
        initialize()
        if (Thread.currentThread().isInterrupted) throw InterruptedException()
        val directory = File(context.filesDir, "media/${task.id}/source").apply { mkdirs() }
        // Probe available subtitle tracks without waiting for the large video transfer.
        val probe = request(task.url, proxy, "subtitle-${task.id}-${task.subtitleGeneration}").apply { addOption("--skip-download"); addOption("--dump-single-json") }
        val info = JSONObject(YoutubeDL.execute(probe, "subtitle-${task.id}-${task.subtitleGeneration}", callback = null).out)
        val language = chooseLanguage(info)
            ?: throw IllegalStateException("此视频没有可用的原字幕或自动字幕；视频下载仍会继续")
        val download = request(task.url, proxy, "subtitle-${task.id}-${task.subtitleGeneration}").apply {
            addOption("--skip-download"); addOption("--write-subs"); addOption("--write-auto-subs")
            addOption("--sub-langs", language); addOption("--sub-format", "srt/vtt/best")
            addOption("--convert-subs", "srt"); addOption("--write-info-json")
            addOption("-o", File(directory, "original.%(ext)s").absolutePath)
        }
        if (Thread.currentThread().isInterrupted) throw InterruptedException()
        YoutubeDL.execute(download, "subtitle-${task.id}-${task.subtitleGeneration}", callback = null)
        val file = directory.listFiles().orEmpty().firstOrNull { it.extension == "srt" && it.length() > 0 }
            ?: throw IllegalStateException("原字幕未能转换为 SRT，请重试字幕任务")
        require(file.length() <= 2 * 1024 * 1024) { "原字幕超过 2 MB，无法提交翻译" }
        val normalized = SrtNormalization.normalize(file.readText(Charsets.UTF_8))
        val output = AtomicFile(File(directory, "source.srt"))
        val stream = output.startWrite()
        try { stream.write(normalized.toByteArray(Charsets.UTF_8)); output.finishWrite(stream) }
        catch (error: Throwable) { output.failWrite(stream); throw error }
        return output.baseFile to info.optString("title").takeIf { it.isNotBlank() }
    }

    private fun chooseLanguage(info: JSONObject): String? {
        fun keys(key: String): List<String> = info.optJSONObject(key)?.keys()?.asSequence()?.filter { it != "live_chat" }?.toList().orEmpty()
        val manual = keys("subtitles")
        val automatic = keys("automatic_captions")
        // Prefer English, then the original spoken language. Avoid choosing an auto-translated track.
        return manual.firstOrNull { it == "en" } ?: manual.firstOrNull { it.startsWith("en-") }
            ?: automatic.firstOrNull { it == "en-orig" } ?: automatic.firstOrNull { it == "en" }
            ?: manual.firstOrNull { it == info.optString("language") }
            ?: manual.firstOrNull() ?: automatic.firstOrNull { it.endsWith("-orig") }
            ?: automatic.firstOrNull { it == info.optString("language") }
            ?: automatic.firstOrNull()
    }

    private fun title(directory: File): String? = runCatching {
        val json = directory.listFiles().orEmpty().first { it.name.endsWith(".info.json") }
        JSONObject(json.readText()).optString("title").takeIf { it.isNotBlank() }
    }.getOrNull()
}

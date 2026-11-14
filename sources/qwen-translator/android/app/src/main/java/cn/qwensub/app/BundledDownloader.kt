package cn.qwensub.app

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.zip.ZipInputStream

/** Upgrade the wrapper's old bundled core before either download worker starts. */
internal object BundledDownloader {
    const val VERSION = "2026.08.19"
    const val SHA256 = "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"
    const val ASSET = "downloader/yt-dlp-$VERSION.zip"
    private val versionPattern = Regex("(?m)^__version__\\s*=\\s*['\"](\\d{4}\\.\\d{2}\\.\\d{2}(?:\\.\\d+)*)['\"]")

    fun ensure(context: Context) = install(coreFile(context)) { context.assets.open(ASSET) }

    fun coreFile(context: Context) = File(context.noBackupFilesDir, "youtubedl-android/yt-dlp/yt-dlp")

    fun version(file: File): String? = runCatching {
        file.inputStream().buffered().use { input ->
            // Official zipimport executables prepend a shebang. Android's ZipFile does not
            // compensate for its offset as desktop Java does; local ZIP headers do not need it.
            input.mark(256)
            if (input.read() == '#'.code && input.read() == '!'.code) {
                var ended = false
                repeat(254) {
                    if (!ended && input.read() == '\n'.code) ended = true
                }
                check(ended) { "Invalid downloader ZIP prefix" }
            } else input.reset()
            ZipInputStream(input).use { archive ->
                var entry = archive.nextEntry
                while (entry != null) {
                    if (entry.name == "yt_dlp/version.py") {
                        return@use versionPattern.find(archive.bufferedReader().readText())?.groupValues?.get(1)
                    }
                    entry = archive.nextEntry
                }
                null
            }
        }
    }.getOrNull()

    private fun digest(file: File): String = file.inputStream().use { input ->
        val hash = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            hash.update(buffer, 0, count)
        }
        hash.digest().joinToString("") { "%02x".format(it) }
    }

    // Called under DownloadEngine's initialization lock; downloads cannot see a half-written zip.
    internal fun install(destination: File, openBundle: () -> InputStream) {
        if (destination.isFile && (digest(destination) == SHA256 ||
                version(destination)?.let { it > VERSION } == true)) return
        val directory = requireNotNull(destination.parentFile)
        check(directory.isDirectory || directory.mkdirs()) { "无法准备下载器目录" }
        val staged = File.createTempFile(".yt-dlp-", ".tmp", directory)
        try {
            FileOutputStream(staged).use { output ->
                openBundle().use { it.copyTo(output) }
                output.fd.sync()
            }
            check(digest(staged) == SHA256 && version(staged) == VERSION) { "安装包中的下载器校验失败，请重新安装" }
            Files.move(staged.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING)
        } finally {
            staged.delete()
        }
    }
}

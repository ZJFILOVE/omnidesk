package cn.qwensub.app

import android.media.MediaExtractor
import android.media.MediaFormat
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.nio.ByteBuffer
import java.security.MessageDigest

/**
 * Opt-in repair of one existing failed video using the app's encrypted saved settings.
 * Arguments: repairDownload=true, taskId=<existing task UUID>, and optionally
 * repairDownloadTimeoutSeconds=1..300 (default 300). Does not create a translation job.
 */
@RunWith(AndroidJUnit4::class)
class DownloadRepairTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private val repository get() = AppRepository.get(context)

    @Test fun bundledCoreChecksumAndVersion() {
        val bytes = context.assets.open(BundledDownloader.ASSET).use { it.readBytes() }
        val actual = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        assertEquals("Bundled core checksum differs on Android", BundledDownloader.SHA256, actual)
        val temporary = File.createTempFile("downloader-verification-", ".zip", context.cacheDir)
        try {
            temporary.writeBytes(bytes)
            assertEquals("Bundled core version cannot be read on Android", BundledDownloader.VERSION,
                BundledDownloader.version(temporary))
        } finally { temporary.delete() }
    }

    @Test fun retryVideoPreservesCompletedSubtitles() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue("Pass -e repairDownload true to retry an existing video",
            arguments.getString("repairDownload") == "true")
        val id = arguments.getString("taskId")?.trim().orEmpty()
        assertTrue("Pass -e taskId with the existing task UUID",
            Regex("[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}").matches(id))
        val timeoutArgument = arguments.getString("repairDownloadTimeoutSeconds")
        val timeoutSeconds = if (timeoutArgument == null) 300L else timeoutArgument.toLongOrNull()
        assertTrue("repairDownloadTimeoutSeconds must be between 1 and 300",
            timeoutSeconds != null && timeoutSeconds in 1L..300L)

        try {
            // Read, but do not replace or print, the encrypted connection settings.
            val savedSettings = repository.settings()
            val original = existingTask(id)
            assertFalse("Task has pending cleanup", original.cleanupPending)
            assertFalse("Task's local files were deleted", original.localFilesDeleted)
            assertNotEquals("Video is already READY; select the failed task", TaskState.READY, original.videoState)
            assertEquals("Subtitles must already be READY", TaskState.READY, original.subtitleState)
            assertNotNull("Completed subtitles must retain their remote job ID", original.remoteId)
            val subtitles = listOf(
                subtitleSnapshot("original", original.originalPath),
                subtitleSnapshot("Chinese", original.zhPath),
                subtitleSnapshot("bilingual", original.bilingualPath),
            )

            val deadline = SystemClock.elapsedRealtime() + timeoutSeconds!! * 1000L
            repository.retryVideo(id)
            while (SystemClock.elapsedRealtime() < deadline) {
                val task = existingTask(id)
                if (task.videoState == TaskState.FAILED) fail("Retried video entered FAILED")
                if (task.videoState == TaskState.CANCELLED) fail("Retried video was cancelled")
                assertPreservedSubtitles(original, task, subtitles)
                assertEquals("Video retry did not advance its generation", original.videoGeneration + 1, task.videoGeneration)
                assertTrue("Video retry changed the saved connection settings", repository.settings() == savedSettings)

                if (task.videoState == TaskState.READY) {
                    assertEquals("Video progress is incomplete", 1f, task.videoProgress, 0f)
                    assertNull("READY video still has an error", task.videoError)
                    val path = task.videoPath
                    assertNotNull("READY video has no local path", path)
                    val file = File(path!!)
                    assertTrue("Downloaded video is missing or empty", file.isFile && file.length() > 0)
                    assertAudioAndVideoSamples(file)
                    // Re-read after media verification to catch concurrent changes to the task.
                    assertPreservedSubtitles(original, existingTask(id), subtitles)
                    assertTrue("Video repair changed the saved connection settings", repository.settings() == savedSettings)
                    return@runBlocking
                }
                delay(1000)
            }
            fail("Existing video did not reach READY within $timeoutSeconds seconds")
        } catch (error: Exception) {
            // Raw exception messages/causes may expose proxy credentials or private file paths.
            throw AssertionError("Existing video repair could not be verified (${error.javaClass.simpleName})")
        }
    }

    private fun existingTask(id: String): DownloadTask = repository.tasks.value.firstOrNull { it.id == id }
        ?: throw AssertionError("Specified existing task was not found")

    private data class SubtitleSnapshot(val label: String, val path: String, val sha256: ByteArray)

    private fun subtitleSnapshot(label: String, path: String?): SubtitleSnapshot {
        assertNotNull("$label subtitle path is missing", path)
        val file = File(path!!)
        assertTrue("$label subtitle file is missing or empty", file.isFile && file.length() > 0)
        assertTrue("$label subtitle is not an SRT file", file.extension.equals("srt", ignoreCase = true))
        return SubtitleSnapshot(label, path, sha256(file))
    }

    private fun assertPreservedSubtitles(original: DownloadTask, task: DownloadTask, subtitles: List<SubtitleSnapshot>) {
        assertEquals("Video retry changed subtitle state", TaskState.READY, task.subtitleState)
        assertEquals("Video retry restarted subtitles", original.subtitleGeneration, task.subtitleGeneration)
        assertTrue("Video retry changed clientRequestId", original.clientRequestId == task.clientRequestId)
        assertTrue("Video retry changed remoteId", original.remoteId == task.remoteId)
        assertTrue("Video retry changed subtitle paths", listOf(task.originalPath, task.zhPath, task.bilingualPath) == subtitles.map { it.path })
        assertTrue("Video retry changed subtitle progress", original.subtitleProgress == task.subtitleProgress)
        assertTrue("Video retry changed subtitle error", original.subtitleError == task.subtitleError)
        assertTrue("Video retry changed NAS job binding", original.nasBaseUrl == task.nasBaseUrl)
        assertEquals("Video retry requested remote translation retry", original.remoteRetryRequested, task.remoteRetryRequested)
        assertEquals("Video retry changed remote file deletion state", original.remoteFilesDeleted, task.remoteFilesDeleted)
        subtitles.forEach { snapshot ->
            val file = File(snapshot.path)
            assertTrue("${snapshot.label} subtitle disappeared", file.isFile)
            assertTrue("${snapshot.label} subtitle content changed", snapshot.sha256.contentEquals(sha256(file)))
        }
    }

    private fun sha256(file: File): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest()
    }

    private fun assertAudioAndVideoSamples(file: File) {
        val extractor = MediaExtractor()
        try {
            extractor.setDataSource(file.absolutePath)
            val tracks = (0 until extractor.trackCount).map { index ->
                index to extractor.getTrackFormat(index).getString(MediaFormat.KEY_MIME).orEmpty()
            }
            val video = tracks.firstOrNull { it.second.startsWith("video/") }?.first
            val audio = tracks.firstOrNull { it.second.startsWith("audio/") }?.first
            assertNotNull("Downloaded media has no readable video track", video)
            assertNotNull("Downloaded media has no readable audio track", audio)
            val buffer = ByteBuffer.allocate(4 * 1024 * 1024)
            listOf(video!!, audio!!).forEach { index ->
                extractor.selectTrack(index)
                extractor.seekTo(0, MediaExtractor.SEEK_TO_CLOSEST_SYNC)
                buffer.clear()
                assertTrue("Downloaded media track contains no readable sample", extractor.readSampleData(buffer, 0) > 0)
                assertTrue("Downloaded media track contains an invalid timestamp", extractor.sampleTime >= 0)
                extractor.unselectTrack(index)
            }
        } finally {
            extractor.release()
        }
    }
}

package cn.qwensub.app

import android.net.Uri
import android.os.SystemClock
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.text.CueGroup
import androidx.media3.exoplayer.ExoPlayer
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Opt-in physical-device tests using a private fixture or the app's encrypted saved settings. */
@RunWith(AndroidJUnit4::class)
class DeviceSmokeTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private val repository get() = AppRepository.get(context)

    @Test fun downloadAndTranslate() = runBlocking {
        val fixture = File(context.filesDir, "smoke-config.json")
        assumeTrue("Seed the private smoke-config.json fixture to run the live test", fixture.isFile)
        val config = JSONObject(fixture.readText())
        repository.saveSettings(ConnectionSettings(config.getString("baseUrl"), config.getString("token"), config.optString("downloadProxy")))
        assertTrue(repository.testConnection().contains("gpt-6-luna"))
        val url = config.optString("url", "https://www.youtube.com/watch?v=jNQXAC9IVRw")
        val id = repository.submit(url)
        File(context.filesDir, "smoke-task-id.txt").writeText(id)
        val deadline = System.currentTimeMillis() + 480_000
        while (System.currentTimeMillis() < deadline) {
            val task = repository.tasks.value.first { it.id == id }
            if (task.videoState == TaskState.FAILED || task.subtitleState == TaskState.FAILED) {
                fail("video=${task.videoError}; subtitle=${task.subtitleError}")
            }
            if (task.videoState == TaskState.READY && task.subtitleState == TaskState.READY) {
                assertTrue(File(task.videoPath!!).length() > 1000)
                assertTrue(File(task.originalPath!!).readText().contains("-->"))
                val bilingual = File(task.bilingualPath!!).readText()
                assertTrue(Regex("[\\u3400-\\u9fff]").containsMatchIn(bilingual))
                assertTrue(Regex("[A-Za-z]{3}").containsMatchIn(bilingual))
                assertNotNull(task.remoteId)
                File(context.filesDir, "smoke-result.json").writeText(repository.diagnostics())
                return@runBlocking
            }
            delay(1000)
        }
        fail("Timed out: ${repository.diagnostics()}")
    }

    /** Run this method explicitly after removing adb reverse routes and configuring the phone. */
    @Test(timeout = 600_000) fun downloadAndTranslateWithoutComputerRelay() = runBlocking {
        assumeTrue("Pass -e directNetworkSmoke true to run this live download and translation test",
            InstrumentationRegistry.getArguments().getString("directNetworkSmoke") == "true")
        val started = SystemClock.elapsedRealtime()
        var phase = "saved_settings"
        var taskId: String? = null
        var healthVerified = false
        var completed = false
        try {
            val settings = repository.settings()
            // Deliberately do not print settings: a wrongly configured proxy could contain credentials.
            assertTrue("Set the saved NAS address to the Tailscale endpoint http://100.90.158.48:8765; USB relay settings are not accepted",
                settings.baseUrl == "http://100.90.158.48:8765")
            assertTrue("Set the saved download proxy to the phone's Clash listener http://127.0.0.1:7890; USB relay settings are not accepted",
                settings.downloadProxy == "http://127.0.0.1:7890")

            phase = "nas_health"
            assertTrue("NAS health did not report the expected translation model", repository.testConnection().contains("gpt-6-luna"))
            healthVerified = true
            phase = "download_and_translation"
            val id = repository.submit("https://www.youtube.com/watch?v=jNQXAC9IVRw")
            taskId = id
            File(context.filesDir, "smoke-task-id.txt").writeText(id)
            val deadline = SystemClock.elapsedRealtime() + 480_000
            while (SystemClock.elapsedRealtime() < deadline) {
                val task = repository.tasks.value.firstOrNull { it.id == id }
                    ?: error("Submitted smoke task disappeared: $id")
                if (task.videoState in setOf(TaskState.FAILED, TaskState.CANCELLED) ||
                    task.subtitleState in setOf(TaskState.FAILED, TaskState.CANCELLED)) {
                    fail("Task $id stopped: video=${task.videoState}; subtitle=${task.subtitleState}; ${repository.diagnostics()}")
                }
                if (task.videoState == TaskState.READY && task.subtitleState == TaskState.READY) {
                    phase = "verify_local_files"
                    assertTrue("Downloaded video is missing or empty", task.videoPath?.let { File(it).length() > 1000 } == true)
                    assertTrue("Original subtitle file is missing or invalid", task.originalPath?.let { File(it).readText().contains("-->") } == true)
                    val bilingual = task.bilingualPath?.let { File(it).readText() }.orEmpty()
                    assertTrue("Bilingual subtitles do not contain Chinese", Regex("[\\u3400-\\u9fff]").containsMatchIn(bilingual))
                    assertTrue("Bilingual subtitles do not contain English", Regex("[A-Za-z]{3}").containsMatchIn(bilingual))
                    assertNotNull("No NAS translation job was created", task.remoteId)
                    assertEquals("Translation used a different NAS route", "http://100.90.158.48:8765", task.nasBaseUrl)
                    completed = true
                    phase = "complete"
                    return@runBlocking
                }
                delay(1000)
            }
            fail("Task $id did not finish within 480 seconds: ${repository.diagnostics()}")
        } catch (error: Exception) {
            // Do not attach the original exception, whose message or cause may contain a URL.
            throw AssertionError("Direct network smoke failed during $phase (task=${taskId ?: "not submitted"}): ${safeError(error)}")
        } finally {
            File(context.filesDir, "smoke-result.json").writeText(JSONObject(repository.diagnostics()).apply {
                put("smoke", JSONObject().apply {
                    put("mode", "tailscale_nas_phone_clash")
                    put("phase", phase)
                    put("task_id", taskId ?: JSONObject.NULL)
                    put("nas_health_verified", healthVerified)
                    put("completed", completed)
                    put("elapsed_ms", SystemClock.elapsedRealtime() - started)
                })
            }.toString(2))
        }
    }

    @Test fun playLocalFilesWithBilingualSubtitles() {
        val task = repository.tasks.value.firstOrNull { it.videoState == TaskState.READY && it.subtitleState == TaskState.READY }
        assumeTrue("Complete a download before local playback verification", task != null)
        val captions = CountDownLatch(1)
        val failure = AtomicReference<String?>()
        var player: ExoPlayer? = null
        instrumentation.runOnMainSync {
            player = ExoPlayer.Builder(context).build().apply {
                volume = 0f
                addListener(object : Player.Listener {
                    override fun onCues(cueGroup: CueGroup) {
                        if (cueGroup.cues.any { Regex("[\\u3400-\\u9fff]").containsMatchIn(it.text?.toString().orEmpty()) }) captions.countDown()
                    }
                    override fun onPlayerError(error: PlaybackException) {
                        failure.set(error.errorCodeName + ": " + error.message)
                        captions.countDown()
                    }
                })
                setMediaItem(MediaItem.Builder().setUri(Uri.fromFile(File(task!!.videoPath!!)))
                    .setSubtitleConfigurations(listOf(MediaItem.SubtitleConfiguration.Builder(Uri.fromFile(File(task.bilingualPath!!)))
                        .setMimeType(MimeTypes.APPLICATION_SUBRIP).setLanguage("zh")
                        .setSelectionFlags(C.SELECTION_FLAG_DEFAULT).build())).build())
                prepare()
                play()
            }
        }
        try {
            assertTrue("No Chinese subtitle cues decoded from local media", captions.await(30, TimeUnit.SECONDS))
            assertNull(failure.get())
        } finally {
            instrumentation.runOnMainSync { player?.release() }
        }
    }

    @Test fun downloadWhileNasOffline() = runBlocking {
        val fixture = File(context.filesDir, "smoke-config.json")
        assumeTrue(fixture.isFile)
        val config = JSONObject(fixture.readText())
        repository.saveSettings(ConnectionSettings(config.getString("baseUrl"), config.getString("token"), config.optString("downloadProxy")))
        // The test operator removes only the NAS adb reverse route; the video proxy stays available.
        val connection = runCatching { repository.testConnection() }
        assertTrue("Remove NAS reverse route before this test", connection.isFailure)
        val marker = File(context.filesDir, "recovery-task-id.txt")
        val existingId = marker.takeIf { it.isFile }?.readText()
        val existing = repository.tasks.value.firstOrNull { it.id == existingId && it.subtitleState != TaskState.READY }
        val id = existing?.id ?: repository.submit(config.getString("url"))
        marker.writeText(id)
        val deadline = System.currentTimeMillis() + 300_000
        while (System.currentTimeMillis() < deadline) {
            val task = repository.tasks.value.first { it.id == id }
            if (task.videoState == TaskState.FAILED || task.subtitleState == TaskState.FAILED) fail(repository.diagnostics())
            if (task.videoState == TaskState.READY && task.subtitleState == TaskState.WAITING && task.originalPath != null) {
                assertNull(task.remoteId)
                File(context.filesDir, "recovery-request-id.txt").writeText(task.clientRequestId)
                return@runBlocking
            }
            delay(1000)
        }
        fail("Independent video download did not finish: ${repository.diagnostics()}")
    }

    @Test fun resumeSubtitlesAfterRestart() = runBlocking {
        val fixture = File(context.filesDir, "smoke-config.json")
        val marker = File(context.filesDir, "recovery-task-id.txt")
        assumeTrue(fixture.isFile && marker.isFile)
        val config = JSONObject(fixture.readText())
        val id = marker.readText()
        val requestId = File(context.filesDir, "recovery-request-id.txt").readText()
        val original = repository.tasks.value.first { it.id == id }
        assertEquals(TaskState.READY, original.videoState)
        assertEquals(requestId, original.clientRequestId)
        repository.saveSettings(ConnectionSettings(config.getString("baseUrl"), config.getString("token"), config.optString("downloadProxy")))
        val deadline = System.currentTimeMillis() + 180_000
        while (System.currentTimeMillis() < deadline) {
            val task = repository.tasks.value.first { it.id == id }
            if (task.subtitleState == TaskState.FAILED) fail(repository.diagnostics())
            if (task.subtitleState == TaskState.READY) {
                assertEquals(original.videoPath, task.videoPath)
                assertEquals(requestId, task.clientRequestId)
                assertNotNull(task.remoteId)
                assertTrue(File(task.bilingualPath!!).isFile)
                return@runBlocking
            }
            delay(1000)
        }
        fail("Subtitle recovery timed out: ${repository.diagnostics()}")
    }

    @Test fun cancelActiveNativeDownloads() = runBlocking {
        val fixture = File(context.filesDir, "smoke-config.json")
        assumeTrue(fixture.isFile)
        val config = JSONObject(fixture.readText())
        repository.saveSettings(ConnectionSettings(config.getString("baseUrl"), config.getString("token"), config.optString("downloadProxy")))
        val id = repository.submit(config.getString("url"))
        fun nativeProcesses(): List<String> = File("/proc").listFiles().orEmpty().mapNotNull { directory ->
            if (directory.name.toIntOrNull() == null) return@mapNotNull null
            runCatching {
                if (android.system.Os.stat(directory.path).st_uid != android.os.Process.myUid()) return@runCatching null
                File(directory, "cmdline").readText().takeIf { it.contains(id) && it.contains("--cache-dir") }
            }.getOrNull()
        }
        val deadline = System.currentTimeMillis() + 30_000
        while (System.currentTimeMillis() < deadline && nativeProcesses().size < 2) delay(100)
        try {
            assertEquals("Both independent native downloads should be running", 2, nativeProcesses().size)
            repository.cancelVideo(id)
            val task = repository.tasks.value.first { it.id == id }
            assertEquals(TaskState.CANCELLED, task.videoState)
            assertNotEquals(TaskState.CANCELLED, task.subtitleState)
        } finally {
            repository.cancelVideo(id)
            repository.cancelSubtitles(id)
        }
        delay(1000)
        assertTrue("Cancelled task left native processes running", nativeProcesses().isEmpty())
    }
}

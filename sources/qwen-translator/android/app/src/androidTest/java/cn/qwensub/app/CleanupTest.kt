package cn.qwensub.app

import android.content.Context
import android.content.ContextWrapper
import android.system.Os
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.json.JSONArray
import java.io.File
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Synthetic fixtures only: never uses the saved repository, NAS settings, or user media. */
@RunWith(AndroidJUnit4::class)
class CleanupTest {
    private lateinit var fixture: File
    private lateinit var context: Context
    private val taskId = "cleanup-${UUID.randomUUID()}"

    @Before fun setUp() {
        val target = InstrumentationRegistry.getInstrumentation().targetContext
        fixture = File(target.cacheDir, "cleanup-fixture-${UUID.randomUUID()}").apply { mkdirs() }
        val isolatedFiles = File(fixture, "files").apply { mkdirs() }
        val isolatedCache = File(fixture, "cache").apply { mkdirs() }
        context = object : ContextWrapper(target) {
            override fun getApplicationContext(): Context = this
            override fun getFilesDir(): File = isolatedFiles
            override fun getCacheDir(): File = isolatedCache
        }
    }

    @After fun tearDown() {
        // All targets of test symlinks are also inside this unique synthetic fixture.
        fixture.deleteRecursively()
    }

    @Test fun removesOwnedMediaAndCacheWhileKeepingOtherTasks() {
        val media = File(context.filesDir, "media/$taskId")
        val video = seed(File(media, "video/video.mp4"))
        seed(File(media, "video/video.mp4.part"))
        seed(File(media, "source/original.srt"))
        seed(File(media, "subtitles/bilingual.srt"))
        val caches = listOf(
            seed(File(context.cacheDir, "yt-cache/video-$taskId-0/cache.json")),
            seed(File(context.cacheDir, "yt-cache/video-$taskId-12/cache.json")),
            seed(File(context.cacheDir, "yt-cache/subtitle-$taskId-2/cache.json")),
        )
        val preserved = listOf(
            seed(File(context.filesDir, "media/$taskId-other/video/video.mp4")),
            seed(File(context.filesDir, "tasks-v1.json")),
            seed(File(context.cacheDir, "yt-cache/video-$taskId-other-0/cache.json")),
            seed(File(context.cacheDir, "yt-cache/subtitle-other-0/cache.json")),
            seed(File(context.cacheDir, "unrelated-cache/cache.json")),
        )

        MediaFiles.deleteTask(context, taskId)

        assertFalse(video.exists())
        assertFalse(media.exists())
        caches.forEach { assertFalse("Owned download cache remains", it.parentFile!!.exists()) }
        preserved.forEach { assertEquals("Another task or app file was modified", "fixture", it.readText()) }
        MediaFiles.deleteTask(context, taskId)
        preserved.forEach { assertTrue("Repeated deletion must remain scoped", it.isFile) }
    }

    @Test fun refusesIdsThatCouldEscapeTheOwnedMediaDirectory() {
        val sentinel = seed(File(context.filesDir, "sentinel.txt"))
        for (id in listOf("", "..", "../sentinel.txt", "a/b", "a\\b", "/absolute", "a%2Fb", "a".repeat(129))) {
            val error = runCatching { MediaFiles.deleteTask(context, id) }.exceptionOrNull()
            assertTrue("Unsafe media ID must be rejected", error is IllegalArgumentException)
        }
        assertEquals("fixture", sentinel.readText())
    }

    @Test fun deletesSymlinksWithoutFollowingTheirTargets() {
        val sentinel = seed(File(fixture, "outside-media/keep.txt"))
        val media = File(context.filesDir, "media/$taskId").apply { mkdirs() }
        seed(File(media, "video/video.mp4"))
        Os.symlink(sentinel.parentFile!!.absolutePath, File(media, "linked-directory").absolutePath)
        Os.symlink(sentinel.absolutePath, File(media, "linked-file").absolutePath)
        val cacheLink = File(context.cacheDir, "yt-cache/video-$taskId-0").apply { parentFile!!.mkdirs() }
        Os.symlink(sentinel.parentFile!!.absolutePath, cacheLink.absolutePath)

        MediaFiles.deleteTask(context, taskId)

        assertEquals("fixture", sentinel.readText())
        assertFalse(media.exists())
        assertFalse(cacheLink.exists())
    }

    @Test fun ownedDirectorySymlinkDoesNotDeleteAnExternalDirectory() {
        val sentinel = seed(File(fixture, "outside-media/keep.txt"))
        val mediaLink = File(context.filesDir, "media/$taskId").apply { parentFile!!.mkdirs() }
        Os.symlink(sentinel.parentFile!!.absolutePath, mediaLink.absolutePath)

        MediaFiles.deleteTask(context, taskId)

        assertEquals("fixture", sentinel.readText())
        assertFalse(mediaLink.exists())
    }

    @Test fun refusesRedirectedMediaAndCacheRoots() {
        for (root in listOf(File(context.filesDir, "media"), File(context.cacheDir, "yt-cache"))) {
            val outside = File(fixture, "outside-${root.name}")
            val sentinel = seed(File(outside, "$taskId/keep.txt"))
            Os.symlink(outside.absolutePath, root.absolutePath)
            try {
                assertNotNull("A redirected storage root must not be accepted",
                    runCatching { MediaFiles.deleteTask(context, taskId) }.exceptionOrNull())
                assertEquals("fixture", sentinel.readText())
            } finally {
                assertTrue(root.delete())
            }
        }
    }

    @Test fun cleanupWaitsForAnExistingWriterAndRemovesItsFinalOutput() {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val deletionStarted = CountDownLatch(1)
        val deletionFinished = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)
        val media = File(context.filesDir, "media/$taskId")
        try {
            val writer = executor.submit {
                MediaFiles.locked(taskId) {
                    seed(File(media, "video/in-progress.part"))
                    entered.countDown()
                    check(release.await(5, TimeUnit.SECONDS)) { "Test writer was not released" }
                    seed(File(media, "video/final.mp4"))
                }
            }
            assertTrue("Writer did not acquire its lock", entered.await(5, TimeUnit.SECONDS))
            val deletion = executor.submit {
                deletionStarted.countDown()
                MediaFiles.deleteTask(context, taskId)
                deletionFinished.countDown()
            }
            assertTrue(deletionStarted.await(5, TimeUnit.SECONDS))
            assertFalse("Cleanup returned while a writer could still recreate files", deletionFinished.await(200, TimeUnit.MILLISECONDS))
            release.countDown()
            writer.get(5, TimeUnit.SECONDS)
            deletion.get(5, TimeUnit.SECONDS)
            assertFalse("The writer's final output must also be deleted", media.exists())
        } finally {
            release.countDown()
            executor.shutdownNow()
            assertTrue("Fixture workers did not stop", executor.awaitTermination(5, TimeUnit.SECONDS))
        }
    }

    @Test fun independentVideoAndSubtitleWritersCanRunTogether() {
        val bothEntered = CountDownLatch(2)
        val release = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)
        try {
            val writers = listOf("video/video.mp4", "subtitles/original.srt").map { path ->
                executor.submit {
                    MediaFiles.locked(taskId) {
                        bothEntered.countDown()
                        check(release.await(5, TimeUnit.SECONDS)) { "Test writer was not released" }
                        seed(File(context.filesDir, "media/$taskId/$path"))
                    }
                }
            }
            assertTrue("Video downloads must not delay independent subtitle processing",
                bothEntered.await(3, TimeUnit.SECONDS))
            release.countDown()
            writers.forEach { it.get(5, TimeUnit.SECONDS) }
        } finally {
            release.countDown()
            executor.shutdownNow()
            assertTrue("Fixture workers did not stop", executor.awaitTermination(5, TimeUnit.SECONDS))
        }
    }

    @Test fun cancelledCoroutineWaitsForActualDownloadReaderBeforeCleanup() = runBlocking {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val cleanupStarted = CountDownLatch(1)
        val cleanupFinished = CountDownLatch(1)
        val executor = Executors.newSingleThreadExecutor()
        val media = File(context.filesDir, "media/$taskId")
        val reader = launch(Dispatchers.Default) {
            runDownload("subtitle-$taskId-0") {
                entered.countDown()
                // Model a native reader that finishes after its coroutine has been cancelled.
                var released = false
                while (!released) {
                    try { released = release.await(5, TimeUnit.SECONDS) }
                    catch (_: InterruptedException) { /* Native completion is controlled by the fixture. */ }
                }
                seed(File(media, "source/late.srt"))
            }
        }
        try {
            assertTrue("Synthetic reader did not start", entered.await(5, TimeUnit.SECONDS))
            reader.cancelAndJoin()
            val cleanup = executor.submit {
                cleanupStarted.countDown()
                DownloadRuns.stopAndJoin(taskId, video = false, subtitle = true)
                MediaFiles.deleteTask(context, taskId)
                cleanupFinished.countDown()
            }
            assertTrue(cleanupStarted.await(5, TimeUnit.SECONDS))
            assertFalse("Coroutine cancellation must not stand in for reader completion",
                cleanupFinished.await(200, TimeUnit.MILLISECONDS))
            release.countDown()
            cleanup.get(5, TimeUnit.SECONDS)
            assertFalse("Late native output must not survive cleanup", media.exists())
        } finally {
            release.countDown()
            reader.cancelAndJoin()
            executor.shutdownNow()
            assertTrue("Fixture workers did not stop", executor.awaitTermination(5, TimeUnit.SECONDS))
        }
    }

    @Test fun taskStorePersistsCleanupIntentAndCompletion() {
        val task = DownloadTask(
            id = taskId,
            url = "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            videoState = TaskState.CANCELLED,
            subtitleState = TaskState.CANCELLED,
            localCleanupPending = true,
            remoteCleanupPending = true,
            cleanupError = "等待连接飞牛",
        )
        TaskStore(context).add(task)
        val restored = TaskStore(context)
        assertEquals(task, restored.get(taskId))
        restored.update(taskId) {
            it.copy(localCleanupPending = false, remoteCleanupPending = false,
                localFilesDeleted = true, remoteFilesDeleted = true, cleanupError = null)
        }
        assertNull(restored.finalizeCleanup(taskId))
        assertNull(restored.get(taskId))
        assertNull(TaskStore(context).get(taskId))
        var staleWorkerRan = false
        assertNull(restored.update(taskId) {
            staleWorkerRan = true
            it.copy(videoState = TaskState.READY, videoPath = "late-output.mp4")
        })
        assertFalse("A removed record must not be recreated by a late worker", staleWorkerRan)
    }

    @Test fun remoteOnlyCleanupHidesHistoryAndPreservesPlayableLocalFiles() {
        val video = seed(File(context.filesDir, "media/$taskId/video/video.mp4"))
        val subtitles = seed(File(context.filesDir, "media/$taskId/subtitles/bilingual.srt"))
        val task = cleanupTask().copy(
            videoState = TaskState.READY, subtitleState = TaskState.READY,
            videoPath = video.absolutePath, bilingualPath = subtitles.absolutePath,
            remoteFilesDeleted = true, remoteId = null,
        )
        val store = TaskStore(context).apply { add(task) }

        val completed = store.finalizeCleanup(taskId)!!

        assertTrue(completed.historyHidden)
        assertTrue(completed.cleanupHistoryHandled)
        assertEquals(task.videoPath, completed.videoPath)
        assertEquals(task.bilingualPath, completed.bilingualPath)
        assertEquals(TaskState.READY, completed.videoState)
        assertEquals("fixture", video.readText())
        assertEquals("fixture", subtitles.readText())
        assertEquals(completed, TaskStore(context).get(taskId))
    }

    @Test fun localOnlyCleanupKeepsRemoteIdentityUntilItsDeletionCompletes() {
        val task = cleanupTask().copy(localFilesDeleted = true)
        val store = TaskStore(context).apply { add(task) }

        val completed = store.finalizeCleanup(taskId)!!

        assertTrue(completed.historyHidden)
        assertEquals(task.clientRequestId, completed.clientRequestId)
        assertEquals(task.remoteId, completed.remoteId)
        assertEquals(task.nasBaseUrl, completed.nasBaseUrl)
        assertEquals(completed, TaskStore(context).get(taskId))
        store.update(taskId) {
            it.copy(remoteCleanupPending = true, historyHidden = false, cleanupHistoryHandled = false)
        }
        assertFalse(store.finalizeCleanup(taskId)!!.historyHidden)
        store.update(taskId) { it.copy(remoteCleanupPending = false, remoteFilesDeleted = true, remoteId = null) }
        assertNull(store.finalizeCleanup(taskId))
        assertNull(TaskStore(context).get(taskId))
    }

    @Test fun partialOrFailedCleanupStaysVisibleAndRetainsItsRetryIdentity() {
        val tasks = listOf(
            cleanupTask().copy(id = "$taskId-pending", localFilesDeleted = true, remoteCleanupPending = true),
            cleanupTask().copy(id = "$taskId-error", localFilesDeleted = true, cleanupError = "飞牛清理失败"),
            cleanupTask().copy(id = "$taskId-no-deletion"),
        )
        val store = TaskStore(context).apply { tasks.forEach { add(it) } }
        tasks.forEach { assertEquals(it, store.finalizeCleanup(it.id)) }

        val restored = TaskStore(context)
        tasks.forEach { task ->
            assertEquals(task, restored.get(task.id))
            assertFalse(restored.get(task.id)!!.historyHidden)
            assertEquals(task.clientRequestId, restored.get(task.id)!!.clientRequestId)
        }
    }

    @Test fun startupMigratesOldCompletedDeletionsWithoutTouchingFailedOrUnrelatedTasks() {
        val video = seed(File(context.filesDir, "media/$taskId-remote/video/video.mp4"))
        val tasks = listOf(
            cleanupTask().copy(id = "$taskId-both", localFilesDeleted = true, remoteFilesDeleted = true),
            cleanupTask().copy(id = "$taskId-local", localFilesDeleted = true),
            cleanupTask().copy(id = "$taskId-remote", remoteFilesDeleted = true,
                videoState = TaskState.READY, videoPath = video.absolutePath),
            cleanupTask().copy(id = "$taskId-pending", localFilesDeleted = true, remoteCleanupPending = true,
                cleanupError = "等待连接飞牛"),
            cleanupTask().copy(id = "$taskId-other"),
        )
        TaskStore(context).apply { tasks.forEach { add(it) } }
        // Model the old on-disk schema, which contains neither history lifecycle field.
        val file = File(context.filesDir, "tasks-v1.json")
        val legacy = JSONArray(file.readText())
        for (index in 0 until legacy.length()) {
            legacy.getJSONObject(index).remove("historyHidden")
            legacy.getJSONObject(index).remove("cleanupHistoryHandled")
        }
        file.writeText(legacy.toString())

        val migrated = TaskStore(context)

        assertNull(migrated.get("$taskId-both"))
        assertTrue(migrated.get("$taskId-local")!!.historyHidden)
        assertTrue(migrated.get("$taskId-remote")!!.historyHidden)
        assertEquals(tasks[3], migrated.get("$taskId-pending"))
        assertEquals(tasks[4], migrated.get("$taskId-other"))
        assertEquals(migrated.tasks.value, TaskStore(context).tasks.value)
    }

    @Test fun localDeletionPrunesOnlyWhenTheTaskNeverHadANasBinding() {
        val store = TaskStore(context)
        val neverSubmitted = cleanupTask().copy(id = "$taskId-unsubmitted", localFilesDeleted = true,
            nasBaseUrl = null, remoteId = null)
        val lostReply = cleanupTask().copy(id = "$taskId-lost-reply", localFilesDeleted = true, remoteId = null)
        val missingRoute = cleanupTask().copy(id = "$taskId-missing-route", localFilesDeleted = true, nasBaseUrl = null)
        listOf(neverSubmitted, lostReply, missingRoute).forEach { store.add(it) }

        assertNull(store.finalizeCleanup(neverSubmitted.id))
        assertTrue(store.finalizeCleanup(lostReply.id)!!.historyHidden)
        assertEquals(lostReply.clientRequestId, store.get(lostReply.id)!!.clientRequestId)
        assertTrue(store.finalizeCleanup(missingRoute.id)!!.historyHidden)
        assertEquals(missingRoute.remoteId, store.get(missingRoute.id)!!.remoteId)
        assertEquals(2, TaskStore(context).tasks.value.size)
    }

    @Test fun nasOfflineCaptionCopiesDoNotRetainAnUnownedRemoteRequestAfterCleanup() {
        val store = TaskStore(context)
        val copied = cleanupTask().copy(id = "$taskId-nas-copy", nasMediaId = "sonarr:file:2", remoteId = null,
            localFilesDeleted = true, subtitleState = TaskState.SKIPPED)
        val translated = copied.copy(id = "$taskId-nas-translated", remoteId = "real-remote-job", subtitleState = TaskState.READY)
        store.add(copied); store.add(translated)
        assertNull(store.finalizeCleanup(copied.id))
        assertTrue(store.finalizeCleanup(translated.id)!!.historyHidden)
        assertEquals(translated.clientRequestId, TaskStore(context).get(translated.id)!!.clientRequestId)
    }

    @Test fun nasSubtitleChoiceAndSkippedStateSurviveRestart() {
        val task = DownloadTask(id = taskId, url = "http://127.0.0.1/v1/media/files/sonarr:file:2/stream",
            nasMediaId = "sonarr:file:2", nasStreamPath = "/v1/media/files/sonarr:file:2/compatible/stream",
            nasSubtitleId = "embedded-2", nasOriginalPath = "/v1/media/files/sonarr:file:2/subtitles/embedded-2",
            subtitleState = TaskState.SKIPPED)
        TaskStore(context).add(task)
        assertEquals(task, TaskStore(context).get(taskId))
    }

    @Test fun mediaCleanupInvalidatesOnlyTheDeletedTranslationRequest() {
        val prefix = "cleanup-request-${UUID.randomUUID()}-"
        val isolated = object : ContextWrapper(context) {
            override fun getSharedPreferences(name: String, mode: Int) = super.getSharedPreferences(prefix + name, mode)
        }
        try {
            val requests = MediaRequestStore(isolated)
            val settings = ConnectionSettings("http://127.0.0.1:8765", "fixture")
            val old = requests.request(settings, "sonarr:file:1", "embedded-2", false, null)
            val newer = requests.request(settings, "sonarr:file:1", "embedded-2", false, null, replace = true)
            val unrelated = requests.request(settings, "sonarr:file:2", "embedded-2", false, null)
            requests.forget(old)
            assertEquals(newer, requests.saved(settings, "sonarr:file:1"))
            requests.forget(newer)
            assertNull(requests.saved(settings, "sonarr:file:1"))
            assertEquals(unrelated, requests.saved(settings, "sonarr:file:2"))
            assertNotEquals(newer, requests.request(settings, "sonarr:file:1", "embedded-2", false, null))
        } finally {
            context.deleteSharedPreferences(prefix + "nas_media_requests")
        }
    }

    @Test fun staleCleanupCannotHideExplicitRetryEvenAfterRestart() {
        val initial = cleanupTask().copy(localFilesDeleted = true, videoGeneration = 2, subtitleGeneration = 3)
        val store = TaskStore(context).apply { add(initial) }
        assertTrue(store.finalizeCleanup(taskId, 2, 3)!!.historyHidden)
        store.update(taskId) {
            it.copy(videoState = TaskState.QUEUED, videoGeneration = 3, localFilesDeleted = false,
                historyHidden = false, cleanupHistoryHandled = true)
        }
        val retry = store.get(taskId)!!

        assertEquals(retry, store.finalizeCleanup(taskId, 2, 3))
        assertEquals(retry, TaskStore(context).get(taskId))

        // Retrying only video after a remote-only deletion leaves the remote deletion marker set.
        val video = seed(File(context.filesDir, "media/$taskId/video/retried.mp4"))
        store.update(taskId) { it.copy(remoteFilesDeleted = true, remoteId = null,
            videoState = TaskState.READY, videoPath = video.absolutePath) }
        val remoteStillDeleted = store.get(taskId)!!
        assertEquals(remoteStillDeleted, store.finalizeCleanup(taskId))
        assertEquals(remoteStillDeleted, TaskStore(context).get(taskId))
        assertFalse(TaskStore(context).get(taskId)!!.historyHidden)
    }

    @Test fun staleGenerationCannotFinalizeANewerCompletedCleanup() {
        val task = cleanupTask().copy(localFilesDeleted = true, remoteFilesDeleted = true,
            videoGeneration = 4, subtitleGeneration = 5)
        val store = TaskStore(context).apply { add(task) }

        assertEquals(task, store.finalizeCleanup(taskId, 3, 5))
        assertEquals(task, store.finalizeCleanup(taskId, 4, 4))
        assertNotNull(store.get(taskId))
        assertNull(store.finalizeCleanup(taskId, 4, 5))
    }

    @Test fun remoteOnlyCleanupRetainsVideoControlsUntilAPlayableDownloadIsReady() {
        val store = TaskStore(context)
        val states = listOf(TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING, TaskState.FAILED, TaskState.CANCELLED)
        val tasks = states.map { state ->
            cleanupTask().copy(id = "$taskId-${state.name}", remoteFilesDeleted = true, remoteId = null,
                videoState = state)
        } + cleanupTask().copy(id = "$taskId-missing", remoteFilesDeleted = true, remoteId = null,
            videoState = TaskState.READY, videoPath = File(fixture, "missing.mp4").absolutePath)
        tasks.forEach { store.add(it) }
        tasks.forEach { task ->
            assertEquals(task, store.finalizeCleanup(task.id))
            assertFalse(store.get(task.id)!!.historyHidden)
            assertFalse(store.get(task.id)!!.cleanupHistoryHandled)
        }
        assertEquals(store.tasks.value, TaskStore(context).tasks.value)

        val waiting = tasks.first { it.videoState == TaskState.WAITING }
        val video = seed(File(context.filesDir, "media/${waiting.id}/video/video.mp4"))
        store.update(waiting.id) { it.copy(videoState = TaskState.READY, videoPath = video.absolutePath) }
        assertTrue(store.finalizeCleanup(waiting.id, waiting.videoGeneration, waiting.subtitleGeneration)!!.historyHidden)
        assertTrue(TaskStore(context).get(waiting.id)!!.historyHidden)
        assertEquals("fixture", video.readText())
    }

    @Test fun retriedCleanupCompletionClearsOldFailureBeforeWorkerCanFinalize() {
        for (localLast in listOf(true, false)) {
            val id = "$taskId-${if (localLast) "local-last" else "remote-last"}"
            val task = cleanupTask().copy(id = id,
                localCleanupPending = localLast, remoteCleanupPending = !localLast,
                localFilesDeleted = !localLast, remoteFilesDeleted = localLast,
                cleanupError = "上次清理失败，等待自动重试")
            val store = TaskStore(context).apply { add(task) }

            // The worker only reports deletion success; it has not reached finalizeCleanup
            // or its final error update when the process is interrupted.
            val completed = store.update(id) {
                if (localLast) it.copy(localCleanupPending = false, localFilesDeleted = true)
                else it.copy(remoteCleanupPending = false, remoteFilesDeleted = true)
            }!!

            assertNull("The successful final deletion must atomically clear the stale error", completed.cleanupError)
            assertFalse(completed.cleanupPending)
            assertNull("Startup must finish removing the completed record", TaskStore(context).get(id))
        }
    }

    @Test fun oneCompletedDeletionDoesNotClearTheOtherLocationsPendingFailure() {
        val task = cleanupTask().copy(localCleanupPending = true, remoteCleanupPending = true,
            cleanupError = "飞牛清理失败")
        val store = TaskStore(context).apply { add(task) }

        val partial = store.update(taskId) { it.copy(localCleanupPending = false, localFilesDeleted = true) }!!

        assertEquals(task.cleanupError, partial.cleanupError)
        assertTrue(partial.remoteCleanupPending)
        assertFalse(partial.historyHidden)
        assertEquals(partial, TaskStore(context).get(taskId))
        // Clearing a flag alone is not evidence of a completed deletion.
        val unconfirmed = store.update(taskId) { it.copy(remoteCleanupPending = false) }!!
        assertEquals(task.cleanupError, unconfirmed.cleanupError)
        assertEquals(unconfirmed, TaskStore(context).get(taskId))
    }

    private fun cleanupTask() = DownloadTask(
        id = taskId, url = "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        videoState = TaskState.CANCELLED, subtitleState = TaskState.CANCELLED,
        nasBaseUrl = "http://192.0.2.1:8765", remoteId = "fixture-remote-id",
    )

    private fun seed(file: File): File = file.apply {
        parentFile!!.mkdirs()
        writeText("fixture")
    }
}

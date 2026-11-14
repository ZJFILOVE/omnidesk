package cn.qwensub.app

import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files

class MediaClientTest {
    private lateinit var server: MockWebServer
    private lateinit var client: MediaClient
    private lateinit var directory: File
    @Before fun start() {
        server = MockWebServer().apply { start() }
        client = MediaClient(ConnectionSettings(server.url("/").toString().trimEnd('/'), "test-media-token", "http://127.0.0.1:1"))
        directory = Files.createTempDirectory("nas-media-test").toFile()
    }
    @After fun stop() { server.shutdown(); directory.deleteRecursively() }

    @Test fun authenticatesLibraryWithoutUsingDownloadProxy() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"configured":true,"items":[],"services":[{"kind":"sonarr","connected":true,"message":""}]}"""))
        assertTrue(client.catalog().configured)
        val request = server.takeRequest()
        assertEquals("/v1/media/catalog", request.path)
        assertEquals("Bearer test-media-token", request.getHeader("Authorization"))
    }

    @Test fun rejectsAbsoluteAndTraversalSubtitlePathsBeforeNetworking() = runBlocking {
        for (path in listOf("https://other.example/v1/media/test", "//other.example/v1/media/test", "/v1/media/../health", "/v1/media/%2e%2e/health", "/v1/media/file?token=other", "/v1/media/a\\b")) {
            assertTrue(runCatching { client.text(path) }.exceptionOrNull() is IllegalArgumentException)
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun refusesCredentialBearingRedirectsAndMasksServerBodies() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", server.url("/elsewhere")))
        assertTrue(runCatching { client.catalog() }.exceptionOrNull() is NasException)
        assertEquals(1, server.requestCount)
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"detail":"token=secret-value"}"""))
        val error = runCatching { client.catalog() }.exceptionOrNull() as NasException
        assertTrue(error.retryable)
        assertFalse(error.message.orEmpty().contains("secret-value"))
    }

    @Test fun translatesSelectedTrackAndForceAsrClearsSubtitleSelection() = runBlocking {
        repeat(2) { server.enqueue(MockResponse().setBody("""{"id":"job-1","state":"queued","media_id":"sonarr:file:4","client_request_id":"stable-request"}""")) }
        val result = client.prepare("sonarr:file:4", "embedded:2", requestId = "stable-request")
        assertEquals("stable-request", result.clientRequestId)
        val request = server.takeRequest()
        assertEquals("/v1/media/files/sonarr:file:4/prepare", request.path)
        assertEquals("embedded:2", JSONObject(request.body.readUtf8()).getString("subtitle_id"))
        client.prepare("sonarr:file:4", "embedded:2", forceAsr = true, audioId = 1, requestId = "stable-request")
        val asr = JSONObject(server.takeRequest().body.readUtf8())
        assertTrue(asr.isNull("subtitle_id")); assertTrue(asr.getBoolean("force_asr")); assertEquals(1, asr.getInt("audio_id"))
    }

    @Test fun resumesPartialDownloadsUsingStrongValidator() = runBlocking {
        val target = File(directory, "episode.mkv")
        File(directory, "episode.mkv.part").writeText("hello ")
        File(directory, "episode.mkv.etag").writeText("\"version-one\"")
        server.enqueue(MockResponse().setResponseCode(206).setHeader("ETag", "\"version-one\"")
            .setHeader("Content-Range", "bytes 6-10/11").setBody("world"))
        client.download("/v1/media/files/sonarr:file:4/stream", target) {}
        assertEquals("hello world", target.readText())
        assertFalse(File(directory, "episode.mkv.part").exists())
        assertFalse(File(directory, "episode.mkv.etag").exists())
        val request = server.takeRequest()
        assertEquals("bytes=6-", request.getHeader("Range"))
        assertEquals("\"version-one\"", request.getHeader("If-Range"))
        assertEquals("identity", request.getHeader("Accept-Encoding"))
    }

    @Test fun staleValidatorsRestartWithoutAppendingNewRepresentation() = runBlocking {
        val target = File(directory, "episode.mkv")
        File(directory, "episode.mkv.part").writeText("old-prefix")
        File(directory, "episode.mkv.etag").writeText("\"old\"")
        server.enqueue(MockResponse().setHeader("ETag", "\"new\"").setBody("new-complete-file"))
        client.download("/v1/media/files/sonarr:file:4/stream", target) {}
        assertEquals("new-complete-file", target.readText())
    }

    @Test fun rejectsWrongRangesWithoutPublishingCorruptFinalFile() = runBlocking {
        val target = File(directory, "episode.mkv")
        File(directory, "episode.mkv.part").writeText("hello ")
        File(directory, "episode.mkv.etag").writeText("\"same\"")
        server.enqueue(MockResponse().setResponseCode(206).setHeader("ETag", "\"same\"")
            .setHeader("Content-Range", "bytes 4-8/9").setBody("wrong"))
        assertTrue(runCatching { client.download("/v1/media/files/sonarr:file:4/stream", target) {} }.exceptionOrNull() is NasException)
        assertFalse(target.exists())
        assertFalse(File(directory, "episode.mkv.part").exists())
    }

    @Test fun completedPartialCanFinishAfterLostFinalResponse() = runBlocking {
        val target = File(directory, "episode.mkv")
        File(directory, "episode.mkv.part").writeText("complete")
        File(directory, "episode.mkv.etag").writeText("\"same\"")
        server.enqueue(MockResponse().setResponseCode(416).setHeader("ETag", "\"same\"").setHeader("Content-Range", "bytes */8"))
        client.download("/v1/media/files/sonarr:file:4/stream", target) {}
        assertEquals("complete", target.readText())
    }

    @Test fun cancellationClosesAStalledNetworkReadPromptly() = runBlocking {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        val worker = launch { client.text("/v1/media/files/sonarr:file:4/subtitles/embedded:1") }
        withTimeout(2_000) { while (server.requestCount == 0) delay(10) }
        worker.cancel()
        withTimeout(2_000) { worker.join() }
    }

    @Test fun compatibilityReusesAuthenticatedMediaRouteAndKeepsOriginalVideoPath() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"state":"queued","progress":0,"message":"排队","stream_path":null,"size":null}"""))
        assertTrue(client.prepareCompatible("sonarr:file:4").active)
        val start = server.takeRequest()
        assertEquals("POST", start.method)
        assertEquals("/v1/media/files/sonarr:file:4/compatible", start.path)
        assertEquals("Bearer test-media-token", start.getHeader("Authorization"))
        server.enqueue(MockResponse().setBody("""{"state":"ready","progress":1,"message":"完成","stream_path":"/v1/media/files/sonarr:file:4/compatible/stream","size":1234}"""))
        val ready = client.compatibility("sonarr:file:4")
        assertTrue(ready.ready)
        val captions = listOf(
            NasMediaSubtitle("embedded-2", "English", "en", "embedded", "/v1/media/files/sonarr:file:4/subtitles/embedded-2"),
            NasMediaSubtitle("generated-job-1-bilingual", "双语", "zh", "generated", "/v1/media/jobs/job-1/subtitles/bilingual"),
        )
        val job = NasMediaJob("job-1", "ready", 1f, "", "", "sonarr:file:4", "request-1")
        val original = NasMediaFile("sonarr:file:4", "Episode", "episode.mp4", 2000, "/v1/media/files/sonarr:file:4/stream", captions, 90.0, emptyList(), job = job)
        val compatible = original.compatibleCopy(ready)
        assertEquals("/v1/media/files/sonarr:file:4/stream", original.streamPath)
        assertEquals("/v1/media/files/sonarr:file:4/compatible/stream", compatible.streamPath)
        assertEquals("episode.mkv", compatible.filename)
        assertEquals(1234L, compatible.size)
        assertEquals(captions, compatible.subtitles)
        assertEquals(job, compatible.job)
        assertEquals("/v1/media/files/sonarr:file:4/subtitles/embedded-2", compatible.subtitles.first().downloadPath)
        assertEquals("GET", server.takeRequest().method)
    }

    @Test fun cleanupRequiresExplicitAcknowledgement() = runBlocking {
        for (body in listOf("""{"status":"queued"}""", "not-json secret")) {
            server.enqueue(MockResponse().setBody(body))
            assertTrue(runCatching { client.deleteRequest("request-1") }.exceptionOrNull() is NasException)
        }
        server.enqueue(MockResponse().setBody("""{"status":"deleted"}"""))
        client.deleteRequest("request-1")
        repeat(3) { assertEquals("DELETE", server.takeRequest().method) }
    }

    @Test fun readsOptionalDefaultRuleAndSearchOutcome() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"root_folders":[],"quality_profiles":[{"id":7,"name":"做种最多优先"}],"default_quality_profile_id":7}"""))
        assertEquals(7, client.options("radarr").defaultQualityProfileId)
        server.takeRequest()
        server.enqueue(MockResponse().setBody("""{"root_folders":[],"quality_profiles":[]}"""))
        assertNull(client.options("radarr").defaultQualityProfileId)
        server.takeRequest()
        server.enqueue(MockResponse().setBody("""{"id":"radarr:movie:5","kind":"movie","title":"Insomnia","search_message":"暂未找到符合规则且有人做种的资源"}"""))
        assertEquals("暂未找到符合规则且有人做种的资源", client.add("radarr", 320, 7, 1).searchMessage)
    }

    @Test fun discoveryEncodesQueriesAndAddUsesActualServiceOptions() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"items":[]}"""))
        client.discover("sonarr", "Law & Order 中文")
        val search = server.takeRequest()
        assertEquals("Law & Order 中文", search.requestUrl?.queryParameter("query"))
        assertEquals("sonarr", search.requestUrl?.queryParameter("kind"))
        server.enqueue(MockResponse().setBody("""{"id":"sonarr:series:2","kind":"series","title":"Law & Order"}"""))
        client.add("sonarr", 12, 3, 4)
        val add = server.takeRequest()
        assertEquals("/v1/media/services/sonarr/add", add.path)
        val body = JSONObject(add.body.readUtf8())
        assertEquals(12, body.getInt("external_id")); assertEquals(3, body.getInt("quality_profile_id")); assertEquals(4, body.getInt("root_folder_id")); assertTrue(body.getBoolean("search"))
    }

    @Test fun managementCapabilitiesRemainOptionalForOlderServices() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"configured":true,"items":[],"services":[]}"""))
        assertTrue(client.catalog().capabilities.isEmpty())
        server.enqueue(MockResponse().setBody("""{"configured":true,"items":[],"services":[],"capabilities":["manage_queue","manage_titles"]}"""))
        assertEquals(setOf("manage_queue", "manage_titles"), client.catalog().capabilities)
    }

    @Test fun queueRemovalUsesOpaqueIdentifierAndDoesNotBlocklistByDefault() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"status":"removed","id":"sonarr:queue:42","message":"removed"}"""))
        client.removeQueueItem("sonarr:queue:42")
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/media/queue/sonarr:queue:42/remove", request.path)
        assertEquals("Bearer test-media-token", request.getHeader("Authorization"))
        val body = JSONObject(request.body.readUtf8())
        assertFalse(body.getBoolean("blocklist"))
        assertFalse(body.has("search"))

        server.enqueue(MockResponse().setBody("""{"status":"removed","id":"radarr:queue:8"}"""))
        client.removeQueueItem("radarr:queue:8", blocklist = true)
        assertTrue(JSONObject(server.takeRequest().body.readUtf8()).getBoolean("blocklist"))
    }

    @Test fun titleRemovalPreservesFilesAndAddsImportExclusionByDefault() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"status":"removed","id":"sonarr:series:2"}"""))
        client.removeTitle("sonarr:series:2")
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/media/titles/sonarr:series:2/remove", request.path)
        val body = JSONObject(request.body.readUtf8())
        assertFalse(body.getBoolean("delete_files"))
        assertTrue(body.getBoolean("add_import_exclusion"))

        server.enqueue(MockResponse().setBody("""{"status":"removed","id":"radarr:movie:8"}"""))
        client.removeTitle("radarr:movie:8", deleteFiles = true, addImportExclusion = false)
        val destructive = JSONObject(server.takeRequest().body.readUtf8())
        assertTrue(destructive.getBoolean("delete_files"))
        assertFalse(destructive.getBoolean("add_import_exclusion"))
    }

    @Test fun removalRequiresMatchingIdentifierAndExplicitRemovedStatus() = runBlocking {
        for (body in listOf(
            """{"status":"queued","id":"sonarr:queue:42"}""",
            """{"status":"removed","id":"sonarr:queue:43"}""",
            """{"status":"removed"}""",
            "not-json token=secret-value",
        )) {
            server.enqueue(MockResponse().setBody(body))
            val error = runCatching { client.removeQueueItem("sonarr:queue:42") }.exceptionOrNull()
            assertTrue(error is NasException)
            assertFalse(error?.message.orEmpty().contains("secret-value"))
        }
        server.enqueue(MockResponse().setBody("""{"status":"deleted","id":"sonarr:series:2"}"""))
        assertTrue(runCatching { client.removeTitle("sonarr:series:2") }.exceptionOrNull() is NasException)
    }

    @Test fun monitoringReturnsUpdatedEpisodesWithoutLaunchingSearch() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"id":"sonarr:series:2","kind":"series","title":"Test","monitored":false,"episodes":[{"id":"sonarr:episode:4","season_number":1,"episode_number":1,"has_file":true,"monitored":false,"media_id":"sonarr:file:4"}]}"""))
        val title = client.setMonitored("sonarr:series:2", false)
        assertFalse(title.monitored)
        assertFalse(title.episodes.single().monitored)
        assertTrue(title.episodes.single().hasFile)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/media/titles/sonarr:series:2/monitor", request.path)
        val body = JSONObject(request.body.readUtf8())
        assertFalse(body.getBoolean("monitored"))
        assertFalse(body.has("search"))
        assertEquals(1, server.requestCount)
    }

    @Test fun monitoringRejectsMissingMismatchedAndMalformedAcknowledgements() = runBlocking {
        for (body in listOf(
            """{"id":"sonarr:series:2","monitored":true}""",
            """{"id":"sonarr:series:3","monitored":false}""",
            """{"id":"sonarr:series:2"}""",
            """{"id":"sonarr:series:2","monitored":"false"}""",
            "not-json",
        )) {
            server.enqueue(MockResponse().setBody(body))
            assertTrue(runCatching { client.setMonitored("sonarr:series:2", false) }.exceptionOrNull() is NasException)
        }
    }

    @Test fun managementRejectsUnsafeIdentifiersBeforeNetworking() = runBlocking {
        for (id in listOf("sonarr:queue:../1", "sonarr%3Aqueue%3A1", "sonarr:queue:1?x=1", "sonarr:queue:1#x", "sonarr:queue:1\\x", "sonarr:queue:1 ")) {
            assertTrue(runCatching { client.removeQueueItem(id) }.exceptionOrNull() is IllegalArgumentException)
            assertTrue(runCatching { client.removeTitle(id) }.exceptionOrNull() is IllegalArgumentException)
            assertTrue(runCatching { client.setMonitored(id, false) }.exceptionOrNull() is IllegalArgumentException)
        }
        assertEquals(0, server.requestCount)
        server.enqueue(MockResponse().setBody("""{"items":[{"id":"sonarr:queue:../1"}]}"""))
        assertTrue(runCatching { client.queue() }.exceptionOrNull() is IllegalArgumentException)
    }

    @Test fun failedManagementRequestsNeverReportSuccessOrRetryAutomatically() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"detail":"token=secret-value"}"""))
        val serviceError = runCatching { client.removeTitle("sonarr:series:2") }.exceptionOrNull() as NasException
        assertTrue(serviceError.retryable)
        assertFalse(serviceError.message.orEmpty().contains("secret-value"))
        assertEquals(1, server.requestCount)
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        assertNotNull(runCatching { client.removeQueueItem("sonarr:queue:42") }.exceptionOrNull())
        assertEquals(2, server.requestCount)
    }

    @Test fun unauthorizedFileDeletionExplainsDirectoryProtectionWithoutLeakingServerDetail() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail":"/private/media/root token=secret-value"}"""))
        val protected = runCatching { client.removeTitle("sonarr:series:2", deleteFiles = true) }.exceptionOrNull() as NasException
        assertTrue(protected.message.orEmpty().contains("目录未获删除授权"))
        assertTrue(protected.message.orEmpty().contains("保留文件移除"))
        assertFalse(protected.message.orEmpty().contains("secret-value"))
        assertFalse(protected.message.orEmpty().contains("/private"))
        assertFalse(protected.retryable)

        server.enqueue(MockResponse().setResponseCode(401))
        val unauthorized = runCatching { client.removeTitle("sonarr:series:2", deleteFiles = true) }.exceptionOrNull() as NasException
        assertTrue(unauthorized.message.orEmpty().contains("访问令牌"))
    }

    @Test fun readRequestsRecoverAfterAReusedConnectionClosesBeforeTheResponse() = runBlocking {
        val catalog = """{"configured":true,"items":[],"services":[]}"""
        server.enqueue(MockResponse().setBody(catalog))
        assertTrue(client.catalog().configured)
        assertEquals(0, server.takeRequest().sequenceNumber)

        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        server.enqueue(MockResponse().setBody(catalog))
        assertTrue(client.catalog().configured)
        assertEquals(1, server.takeRequest().sequenceNumber)
        assertEquals(0, server.takeRequest().sequenceNumber)
        assertEquals(3, server.requestCount)
    }

    @Test fun writesUseFreshConnectionsSeparateFromTheReusableReadConnection() = runBlocking {
        val catalog = """{"configured":true,"items":[],"services":[]}"""
        server.enqueue(MockResponse().setBody(catalog))
        client.catalog()
        assertEquals(0, server.takeRequest().sequenceNumber)

        repeat(2) {
            server.enqueue(MockResponse().setBody("""{"status":"removed","id":"sonarr:queue:42"}"""))
            client.removeQueueItem("sonarr:queue:42")
            assertEquals(0, server.takeRequest().sequenceNumber)
        }

        server.enqueue(MockResponse().setBody(catalog))
        client.catalog()
        assertEquals(1, server.takeRequest().sequenceNumber)
    }
}

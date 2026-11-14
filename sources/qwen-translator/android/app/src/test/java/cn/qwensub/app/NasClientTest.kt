package cn.qwensub.app

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

class NasClientTest {
    private lateinit var server: MockWebServer
    private lateinit var client: NasClient
    @Before fun start() {
        server = MockWebServer().apply { start() }
        // Invalid download proxy must never affect NAS requests.
        client = NasClient(ConnectionSettings(server.url("/").toString().trimEnd('/'), "unit-test-token", "http://127.0.0.1:1"))
    }
    @After fun stop() { server.shutdown() }

    @Test fun authenticatesHealthAndChecksProtocolVersion() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"status":"ok","api_version":1,"model":"gpt-6-luna"}"""))
        assertTrue(client.health().contains("gpt-6-luna"))
        val request = server.takeRequest()
        assertEquals("/health", request.path)
        assertEquals("Bearer unit-test-token", request.getHeader("Authorization"))
        server.enqueue(MockResponse().setBody("""{"status":"ok","api_version":2}"""))
        val error = runCatching { client.health() }.exceptionOrNull() as NasException
        assertFalse(error.retryable)
    }

    @Test fun repeatsPersistedRequestIdAfterLostReply() = runBlocking {
        val task = DownloadTask(url = "https://www.youtube.com/watch?v=jNQXAC9IVRw", title = "Test")
        repeat(2) { server.enqueue(MockResponse().setBody("""{"id":"remote-1","state":"queued","progress":0,"model":"gpt-6-luna"}""")) }
        assertEquals("remote-1", client.submit(task, "1\n00:00:00,000 --> 00:00:01,000\nHello").id)
        assertEquals("remote-1", client.submit(task, "1\n00:00:00,000 --> 00:00:01,000\nHello").id)
        repeat(2) {
            val request = server.takeRequest()
            assertEquals("POST", request.method); assertEquals("/v1/jobs", request.path)
            val body = JSONObject(request.body.readUtf8())
            assertEquals(task.clientRequestId, body.getString("client_request_id"))
            assertEquals(task.url, body.getString("source_url"))
            assertTrue(body.getString("subtitle_srt").contains("Hello"))
        }
    }

    @Test fun doesNotFollowCredentialBearingRedirects() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(302).setHeader("Location", server.url("/other")))
        val error = runCatching { client.health() }.exceptionOrNull() as NasException
        assertFalse(error.retryable)
        assertEquals(1, server.requestCount)
    }

    @Test fun onlyTransientHttpErrorsRetryAndServerBodyCannotLeakSecrets() = runBlocking {
        for ((code, expectedRetry) in listOf(401 to false, 404 to false, 429 to true, 503 to true)) {
            server.enqueue(MockResponse().setResponseCode(code).setBody("token=secret"))
            val error = runCatching { client.status("remote-1") }.exceptionOrNull() as NasException
            assertEquals(expectedRetry, error.retryable)
            assertFalse(error.message.orEmpty().contains("secret"))
        }
    }

    @Test fun rejectsRemoteIdPathTraversal() = runBlocking {
        val error = runCatching { client.status("../health") }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
        assertEquals(0, server.requestCount)
    }

    @Test fun deletesByPersistedRequestIdAndCanRepeatAfterLostReply() = runBlocking {
        repeat(2) {
            enqueueCleanupCapability()
            server.enqueue(MockResponse().setBody("""{"status":"deleted"}"""))
            client.deleteRequest("persisted-request_1")
            val health = server.takeRequest()
            assertEquals("GET", health.method)
            assertEquals("/health", health.path)
            assertEquals("Bearer unit-test-token", health.getHeader("Authorization"))
            val deletion = server.takeRequest()
            assertEquals("DELETE", deletion.method)
            assertEquals("/v1/requests/persisted-request_1", deletion.path)
            assertEquals("Bearer unit-test-token", deletion.getHeader("Authorization"))
            assertEquals(0L, deletion.bodySize)
        }
    }

    @Test fun refusesCleanupOnOldOrIncompatibleServers() = runBlocking {
        for (response in listOf(
            """{"status":"ok","api_version":1}""",
            """{"status":"ok","api_version":1,"capabilities":["cancel_jobs"]}""",
            """{"status":"ok","api_version":2,"capabilities":["delete_requests"]}""",
            """{"status":"unavailable","api_version":1,"capabilities":["delete_requests"]}""",
        )) {
            server.enqueue(MockResponse().setBody(response))
            val before = server.requestCount
            val error = runCatching { client.deleteRequest("request-1") }.exceptionOrNull()
            assertTrue("Expected an explicit compatibility failure", error is NasException)
            assertFalse((error as NasException).retryable)
            assertEquals("Unsupported servers must never receive a DELETE", before + 1, server.requestCount)
            assertEquals("/health", server.takeRequest().path)
        }
    }

    @Test fun missingDeleteRouteNeverCountsAsAlreadyDeleted() = runBlocking {
        enqueueCleanupCapability()
        server.enqueue(MockResponse().setResponseCode(404).setBody("token=secret"))
        val error = runCatching { client.deleteRequest("request-1") }.exceptionOrNull() as NasException
        assertFalse(error.retryable)
        assertFalse(error.message.orEmpty().contains("secret"))
        assertEquals(2, server.requestCount)
    }

    @Test fun requiresExplicitDeletionAcknowledgement() = runBlocking {
        for (response in listOf(
            """{"status":"queued"}""",
            """{"status":"ok"}""",
            """{"message":"secret"}""",
            "not-json secret",
            "",
        )) {
            enqueueCleanupCapability()
            server.enqueue(MockResponse().setBody(response))
            val error = runCatching { client.deleteRequest("request-1") }.exceptionOrNull()
            assertTrue("Only an explicit deleted response is success", error is NasException)
            assertFalse((error as NasException).retryable)
            assertFalse(error.message.orEmpty().contains("secret"))
        }
    }

    @Test fun deletionOnlyRetriesTransientFailures() = runBlocking {
        for ((code, expectedRetry) in listOf(401 to false, 403 to false, 405 to false, 408 to true, 429 to true, 503 to true)) {
            enqueueCleanupCapability()
            // Prevent OkHttp's immediate 408 follow-up so this checks the worker's retry policy.
            server.enqueue(MockResponse().setResponseCode(code).setHeader("Retry-After", "1").setBody("token=secret"))
            val error = runCatching { client.deleteRequest("request-1") }.exceptionOrNull() as NasException
            assertEquals(expectedRetry, error.retryable)
            assertFalse(error.message.orEmpty().contains("secret"))
        }
    }

    @Test fun rejectsUnsafeCleanupIdsBeforeAnyNetworkRequest() = runBlocking {
        for (id in listOf("", "../health", "a/b", "a?delete=all", "a%2Fb", "a\\b", "a\n", "a".repeat(129))) {
            val error = runCatching { client.deleteRequest(id) }.exceptionOrNull()
            assertTrue("Unsafe request ID must be rejected: ${id.length} characters", error is IllegalArgumentException)
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun cleanupDoesNotFollowCredentialBearingRedirects() = runBlocking {
        enqueueCleanupCapability()
        server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", server.url("/elsewhere")))
        val error = runCatching { client.deleteRequest("request-1") }.exceptionOrNull() as NasException
        assertFalse(error.retryable)
        assertEquals(2, server.requestCount)
    }

    private fun enqueueCleanupCapability() {
        server.enqueue(MockResponse().setBody("""{"status":"ok","api_version":1,"capabilities":["delete_requests"]}"""))
    }
}

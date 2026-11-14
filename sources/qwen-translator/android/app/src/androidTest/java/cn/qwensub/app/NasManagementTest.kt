package cn.qwensub.app

import android.app.Application
import android.content.Context
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.AndroidJUnitRunner
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.Assume.assumeTrue
import org.junit.runner.RunWith
import java.net.InetAddress
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Opt-in runner for -PnasManagementUiTest=true. A plain Application prevents the
 * production Application from recovering real tasks or loading saved connections.
 */
class NasManagementTestRunner : AndroidJUnitRunner() {
    override fun newApplication(cl: ClassLoader, className: String, context: Context): Application =
        super.newApplication(cl, Application::class.java.name, context)
}

/** Uses only a loopback fixture and injected settings; never contacts the user's NAS. */
@RunWith(AndroidJUnit4::class)
class NasManagementTest {
    @get:Rule val compose = createComposeRule()
    private lateinit var server: MockWebServer
    private val requests = CopyOnWriteArrayList<ObservedRequest>()
    private val queueRemoved = AtomicBoolean(false)
    private val titleRemoved = AtomicBoolean(false)
    private val monitored = AtomicBoolean(true)
    private var advertiseManagement = true
    private var failQueueRemoval = false

    private data class ObservedRequest(val method: String, val path: String, val body: String)

    @Before fun start() {
        assumeTrue("Build this test APK with -PnasManagementUiTest=true",
            Application::class.java == InstrumentationRegistry.getInstrumentation().targetContext.applicationContext.javaClass)
        server = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    val path = request.requestUrl?.encodedPath.orEmpty()
                    val body = request.body.readUtf8()
                    requests += ObservedRequest(request.method.orEmpty(), path, body)
                    if (request.getHeader("Authorization") != "Bearer isolated-ui-test-token") return json("{}", 401)
                    val response = when {
                        request.method == "GET" && path == "/v1/media/catalog" -> catalog()
                        request.method == "GET" && path == "/v1/media/queue" -> JSONObject().put("items",
                            JSONArray().also { if (!queueRemoved.get()) it.put(queueItem()) }).toString()
                        request.method == "GET" && path == "/v1/media/titles/sonarr:series:7" -> title().toString()
                        request.method == "POST" && path == "/v1/media/queue/sonarr:queue:9/remove" -> {
                            if (failQueueRemoval) return json("{}", 503)
                            queueRemoved.set(true)
                            JSONObject().put("status", "removed").put("id", "sonarr:queue:9").toString()
                        }
                        request.method == "POST" && path == "/v1/media/titles/sonarr:series:7/remove" -> {
                            titleRemoved.set(true)
                            JSONObject().put("status", "removed").put("id", "sonarr:series:7").toString()
                        }
                        request.method == "POST" && path == "/v1/media/titles/sonarr:series:7/monitor" -> {
                            monitored.set(JSONObject(body).getBoolean("monitored"))
                            title().toString()
                        }
                        else -> return json("{}", 404)
                    }
                    return json(response)
                }
            }
            start(InetAddress.getByName("127.0.0.1"), 0)
        }
    }

    @After fun stop() { if (::server.isInitialized) server.shutdown() }

    @Test fun queueCancelSendsNothingAndConfirmationRemovesThenRefreshes() {
        openQueue()
        click("删除任务")
        inDialog("以后不再下载这个资源").assertIsOff()
        inDialog("取消").performClick()
        compose.onNode(isDialog()).assertDoesNotExist()
        assertTrue(mutations().isEmpty())

        click("删除任务")
        inDialog("删除任务").performClick()
        waitText("当前没有下载任务")
        assertFalse(JSONObject(mutations().single().body).getBoolean("blocklist"))
        assertEquals("/v1/media/queue/sonarr:queue:9/remove", mutations().single().path)
        compose.waitUntil(5_000) { requests.count { it.method == "GET" && it.path == "/v1/media/queue" } >= 2 }
        compose.onNodeWithText("测试下载任务").assertDoesNotExist()
    }

    @Test fun queueFailureKeepsConfirmationOpenAndTaskVisible() {
        failQueueRemoval = true
        openQueue()
        click("删除任务")
        inDialog("删除任务").performClick()
        waitText("飞牛媒体服务尚未配置或暂时无法连接，请检查 Radarr / Sonarr 和媒体目录。")
        compose.onNode(isDialog()).assertExists()
        inDialog("删除任务").assertIsEnabled()
        assertEquals(1, mutations().size)
        inDialog("取消").performClick()
        compose.onNodeWithText("测试下载任务").assertExists()
        assertFalse(queueRemoved.get())
    }

    @Test fun removingTitleKeepsNasFilesByDefaultAndRefreshesCatalog() {
        openTitle()
        click("移除作品")
        inDialog("同时删除飞牛上的视频文件").assertIsOff()
        inDialog("以后不要自动重新添加").assertIsOn()
        inDialog("移除作品").performClick()
        waitText("片库还没有剧集")
        val body = JSONObject(mutations().single().body)
        assertFalse(body.getBoolean("delete_files"))
        assertTrue(body.getBoolean("add_import_exclusion"))
        assertEquals("/v1/media/titles/sonarr:series:7/remove", mutations().single().path)
        compose.onNode(isDialog()).assertDoesNotExist()
    }

    @Test fun deletingNasFilesRequiresExplicitCheckboxSelection() {
        openTitle()
        click("移除作品")
        inDialog("同时删除飞牛上的视频文件").performClick().assertIsOn()
        inDialog("移除并删除视频").performClick()
        waitText("片库还没有剧集")
        assertTrue(JSONObject(mutations().single().body).getBoolean("delete_files"))
    }

    @Test fun monitoringRequiresConfirmationAndUpdatesTheDetail() {
        openTitle()
        click("停止自动追更")
        inDialog("取消").performClick()
        assertTrue(mutations().isEmpty())
        click("停止自动追更")
        inDialog("停止自动追更").performClick()
        waitText("恢复自动追更")
        val request = mutations().single()
        assertEquals("/v1/media/titles/sonarr:series:7/monitor", request.path)
        assertFalse(JSONObject(request.body).getBoolean("monitored"))
        compose.waitUntil(5_000) { requests.count { it.method == "GET" && it.path == "/v1/media/titles/sonarr:series:7" } >= 2 }
        compose.onNode(isDialog()).assertDoesNotExist()
    }

    @Test fun legacyServiceWithoutCapabilitiesShowsUpgradeAndHidesActions() {
        advertiseManagement = false
        showLibrary()
        click("正在下载")
        waitText("更新飞牛服务以管理下载任务")
        compose.onNodeWithText("删除任务").assertDoesNotExist()
        click("剧集")
        click("测试剧集")
        waitText("更新飞牛服务以管理作品")
        compose.onNodeWithText("移除作品").assertDoesNotExist()
        compose.onNodeWithText("停止自动追更").assertDoesNotExist()
        assertTrue(mutations().isEmpty())
    }

    private fun showLibrary() {
        val settings = ConnectionSettings(server.url("/").toString().trimEnd('/'), "isolated-ui-test-token")
        compose.setContent {
            FeiniuTheme(darkTheme = false) {
                NasLibraryScreen(settings, onSettings = {}, onPlay = {}, onDownload = {}, notify = {})
            }
        }
        waitText("测试剧集")
    }

    private fun openQueue() { showLibrary(); click("正在下载"); waitText("测试下载任务") }
    private fun openTitle() { showLibrary(); click("测试剧集"); waitText("停止自动追更") }

    private fun click(text: String) {
        compose.onNode(hasScrollToIndexAction()).performScrollToNode(hasText(text))
        compose.onNodeWithText(text).performClick()
    }

    private fun waitText(text: String) {
        compose.waitUntil(5_000) { compose.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty() }
    }

    private fun inDialog(text: String) = compose.onNode(hasText(text) and hasAnyAncestor(isDialog()))
    private fun mutations() = requests.filter { it.method != "GET" }
    private fun json(body: String, code: Int = 200) = MockResponse().setResponseCode(code)
        .setHeader("Content-Type", "application/json").setBody(body)

    private fun catalog() = JSONObject().put("configured", true).put("services", JSONArray()
        .put(JSONObject().put("kind", "sonarr").put("connected", true).put("message", "")))
        .put("items", JSONArray().also { if (!titleRemoved.get()) it.put(title()) })
        .also { if (advertiseManagement) it.put("capabilities", JSONArray(listOf("manage_queue", "manage_titles"))) }.toString()

    private fun title() = JSONObject().put("id", "sonarr:series:7").put("kind", "series")
        .put("title", "测试剧集").put("year", 2026).put("overview", "")
        .put("monitored", monitored.get()).put("available_count", 0).put("total_count", 1)
        .put("episodes", JSONArray().put(JSONObject().put("id", "sonarr:episode:11")
            .put("title", "测试单集").put("season_number", 1).put("episode_number", 1)
            .put("has_file", false).put("monitored", monitored.get())))

    private fun queueItem() = JSONObject().put("id", "sonarr:queue:9").put("kind", "sonarr")
        .put("title", "测试下载任务").put("status", "downloading").put("progress", 0.4)
        .put("size", 1000).put("size_left", 600).put("time_left", "00:10:00")
}

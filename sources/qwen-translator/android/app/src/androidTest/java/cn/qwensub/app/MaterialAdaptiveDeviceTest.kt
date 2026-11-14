package cn.qwensub.app

import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Subtitles
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.semantics.ProgressBarRangeInfo
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Runs in an isolated Compose host. Never opens user media, credentials or repositories. */
@RunWith(AndroidJUnit4::class)
class MaterialAdaptiveDeviceTest {
    @get:Rule val ui = createComposeRule()

    @Test fun navigationChangesTheActiveContent() {
        ui.setContent {
            FeiniuTheme(darkTheme = true) {
                var selected by remember { mutableIntStateOf(0) }
                val labels = listOf("首页", "家庭影库", "我的下载", "设置")
                MaterialAppScaffold(labels.map { MaterialDestination(it, Icons.Outlined.Subtitles) },
                    selected, { selected = it }, topBar = {}, snackbarHost = {}) { padding ->
                    Text("当前：${labels[selected]}", Modifier.padding(padding))
                }
            }
        }
        ui.onNodeWithText("我的下载").performClick()
        ui.onNodeWithText("当前：我的下载").assertIsDisplayed()
        ui.onNodeWithText("设置").performClick()
        ui.onNodeWithText("当前：设置").assertIsDisplayed()
    }

    @Test fun wideLayoutSupportsSelectionAndTouchResizing() {
        val tasks = listOf(DownloadTask(id = "first", title = "第一条", url = "fixture"),
            DownloadTask(id = "second", title = "第二条", url = "fixture"))
        ui.setContent {
            FeiniuTheme(darkTheme = true) {
                MaterialTaskLayout(tasks, detail = { Text("详情：${it.id}") }) { _, select ->
                    Column { tasks.forEach { task -> Button(onClick = { select?.invoke(task.id) }) { Text(task.title) } } }
                }
            }
        }
        ui.onNodeWithText("详情：first").assertIsDisplayed()
        ui.onNodeWithText("第二条").performClick()
        ui.onNodeWithText("详情：second").assertIsDisplayed()
        val separator = ui.onNodeWithContentDescription("调整列表与详情宽度")
        val before = separator.fetchSemanticsNode().config[SemanticsProperties.ProgressBarRangeInfo].current
        separator.performTouchInput { swipe(center, center + Offset(100f, 0f), 400) }
        ui.waitForIdle()
        val after = separator.fetchSemanticsNode().config[SemanticsProperties.ProgressBarRangeInfo].current
        assertTrue("Touch drag did not resize the panes", after > before)
    }

    @Test fun narrowSplitScreenUsesOnePane() {
        ui.setContent {
            FeiniuTheme(darkTheme = false) {
                Box(Modifier.width(400.dp).fillMaxHeight()) {
                    MaterialTaskLayout(emptyList(), detail = { Text("不应出现的详情") }) { _, _ -> Text("单栏列表") }
                }
            }
        }
        ui.onNodeWithText("单栏列表").assertIsDisplayed()
        ui.onNodeWithContentDescription("调整列表与详情宽度").assertDoesNotExist()
    }
}

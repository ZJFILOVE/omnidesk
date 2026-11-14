package cn.qwensub.app

import androidx.compose.foundation.background
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Subtitles
import androidx.compose.material.icons.outlined.TouchApp
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.key.*
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.*
import androidx.compose.ui.unit.dp

internal data class MaterialDestination(val label: String, val icon: ImageVector)

@Composable
internal fun MaterialAppScaffold(
    destinations: List<MaterialDestination>, selected: Int, select: (Int) -> Unit,
    topBar: @Composable () -> Unit, snackbarHost: @Composable () -> Unit,
    content: @Composable (PaddingValues) -> Unit,
) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val navigation = materialNavigation(maxWidth.value)
        Row(Modifier.fillMaxSize()) {
            if (navigation != MaterialNavigation.BOTTOM) {
                if (navigation == MaterialNavigation.RAIL) {
                    NavigationRail(containerColor = ReadingColors.Paper, modifier = Modifier.fillMaxHeight()) {
                        Icon(Icons.Outlined.Subtitles, contentDescription = "飞牛字幕", tint = ReadingColors.CaptionBlue,
                            modifier = Modifier.padding(vertical = 20.dp).size(28.dp))
                        destinations.forEachIndexed { index, item ->
                            NavigationRailItem(selected = index == selected, onClick = { select(index) },
                                icon = { Icon(item.icon, contentDescription = null) }, label = { Text(item.label) },
                                colors = NavigationRailItemDefaults.colors(indicatorColor = ReadingColors.BlueWash))
                        }
                    }
                } else {
                    Column(Modifier.width(MaterialTokens.LayoutSidebarWidth).fillMaxHeight().background(ReadingColors.Paper)
                        .windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Start + WindowInsetsSides.Top))
                        .verticalScroll(rememberScrollState()).padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Row(Modifier.padding(horizontal = 12.dp, vertical = 20.dp), verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                            Icon(Icons.Outlined.Subtitles, contentDescription = null, tint = ReadingColors.CaptionBlue)
                            Text("飞牛字幕", style = MaterialTheme.typography.titleLarge, color = ReadingColors.Ink)
                        }
                        Text("浏览", Modifier.padding(12.dp), color = ReadingColors.SecondaryInk, style = MaterialTheme.typography.labelMedium)
                        destinations.forEachIndexed { index, item ->
                            NavigationDrawerItem(label = { Text(item.label) }, selected = index == selected, onClick = { select(index) },
                                icon = { Icon(item.icon, contentDescription = null) },
                                colors = NavigationDrawerItemDefaults.colors(selectedContainerColor = ReadingColors.BlueWash,
                                    unselectedContainerColor = Color.Transparent))
                        }
                    }
                }
                VerticalDivider(color = ReadingColors.Border)
            }
            Scaffold(
                modifier = Modifier.weight(1f), containerColor = ReadingColors.Paper,
                topBar = topBar, snackbarHost = snackbarHost,
                bottomBar = {
                    if (navigation == MaterialNavigation.BOTTOM) NavigationBar(containerColor = ReadingColors.Paper, tonalElevation = 0.dp) {
                        destinations.forEachIndexed { index, item ->
                            NavigationBarItem(selected = index == selected, onClick = { select(index) },
                                icon = { Icon(item.icon, contentDescription = null) }, label = { Text(item.label) },
                                colors = NavigationBarItemDefaults.colors(indicatorColor = ReadingColors.BlueWash))
                        }
                    }
                }, content = content,
            )
        }
    }
}

@Composable
internal fun MaterialSplit(master: @Composable () -> Unit, detail: @Composable () -> Unit) {
    var fraction by rememberSaveable { mutableFloatStateOf(0.44f) }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val widthPx = with(LocalDensity.current) { maxWidth.toPx() }
        Row(Modifier.fillMaxSize()) {
            Box(Modifier.weight(fraction).fillMaxHeight()) { master() }
            Box(Modifier.width(48.dp).fillMaxHeight()
                .semantics {
                    contentDescription = "调整列表与详情宽度"
                    progressBarRangeInfo = ProgressBarRangeInfo(fraction, 0.32f..0.58f)
                    setProgress { fraction = materialPaneFraction(it); true }
                    customActions = listOf(CustomAccessibilityAction("恢复默认宽度") { fraction = 0.44f; true })
                }
                .onPreviewKeyEvent {
                    if (it.type != KeyEventType.KeyDown) false else when (it.key) {
                        Key.DirectionLeft -> { fraction = materialPaneFraction(fraction - 0.03f); true }
                        Key.DirectionRight -> { fraction = materialPaneFraction(fraction + 0.03f); true }
                        Key.MoveHome -> { fraction = 0.44f; true }
                        else -> false
                    }
                }.focusable()
                .draggable(rememberDraggableState { delta -> fraction = materialPaneFraction(fraction + delta / widthPx.coerceAtLeast(1f)) }, Orientation.Horizontal),
                contentAlignment = Alignment.Center,
            ) {
                Surface(color = ReadingColors.Border, shape = MaterialTheme.shapes.small, modifier = Modifier.width(4.dp).height(40.dp)) {}
            }
            Box(Modifier.weight(1f - fraction).fillMaxHeight()) { detail() }
        }
    }
}

@Composable
internal fun MaterialTaskLayout(
    tasks: List<DownloadTask>, detail: @Composable (DownloadTask) -> Unit,
    list: @Composable (String?, ((String) -> Unit)?) -> Unit,
) {
    var selectedId by rememberSaveable { mutableStateOf<String?>(null) }
    val selected = tasks.firstOrNull { it.id == selectedId } ?: tasks.firstOrNull()
    val detailScroll = rememberScrollState()
    LaunchedEffect(selected?.id) { detailScroll.scrollTo(0) }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        if (materialTwoPane(maxWidth.value, maxHeight.value)) {
            MaterialSplit(master = { list(selected?.id) { selectedId = it } }, detail = {
                Column(Modifier.fillMaxSize().verticalScroll(detailScroll).padding(top = 12.dp, end = 20.dp, bottom = 20.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp)) {
                    MaterialSectionTitle("视频详情", "查看下载和字幕进度。")
                    if (selected != null) key(selected.id) { detail(selected) }
                    else MaterialPanel {
                        Column(Modifier.fillMaxWidth().padding(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Icon(Icons.Outlined.TouchApp, contentDescription = null, tint = ReadingColors.CaptionBlue)
                            Text("选择一个视频", style = MaterialTheme.typography.titleMedium)
                            Text("添加视频后，可以在这里查看进度。", color = ReadingColors.SecondaryInk)
                        }
                    }
                }
            })
        } else list(null, null)
    }
}

@Composable
internal fun MaterialTaskSummary(task: DownloadTask, selected: Boolean, onClick: () -> Unit) {
    Surface(onClick = onClick, color = if (selected) ReadingColors.BlueWash else ReadingColors.Sheet, shadowElevation = if (selected) 0.dp else 1.dp,
        shape = MaterialTheme.shapes.large, modifier = Modifier.fillMaxWidth().semantics { this.selected = selected }) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(task.title, style = MaterialTheme.typography.titleMedium, maxLines = 2)
            Text(task.category().label, style = MaterialTheme.typography.labelMedium, color = ReadingColors.CaptionBlue)
            Text("视频：${stateLabel(task.videoState, task.videoProgress)}\n字幕：${stateLabel(task.subtitleState, task.subtitleProgress)}",
                style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
        }
    }
}

@Composable
internal fun MaterialPlayerBody(video: @Composable (Modifier) -> Unit, controls: @Composable () -> Unit) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
        if (materialTwoPane(maxWidth.value, maxHeight.value)) {
            Row(Modifier.fillMaxSize()) {
                Box(Modifier.weight(1f).fillMaxHeight().background(Color.Black), contentAlignment = Alignment.Center) {
                    video(Modifier.fillMaxWidth().aspectRatio(16f / 9f))
                }
                Column(Modifier.width(344.dp).fillMaxHeight().verticalScroll(rememberScrollState())) { controls() }
            }
        } else Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
            video(Modifier.fillMaxWidth().aspectRatio(16f / 9f))
            controls()
        }
    }
}

package cn.qwensub.app

import android.Manifest
import android.content.Intent
import android.content.res.Configuration
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.view.WindowCompat
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.OpenInNew
import androidx.compose.material.icons.automirrored.outlined.PlaylistPlay
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : ComponentActivity() {
    private var sharedText by mutableStateOf("")

    override fun onCreate(savedInstanceState: Bundle?) {
        val appearance = AppearanceSettings(this)
        val initialMode = appearance.readTheme()
        val systemDark = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK == Configuration.UI_MODE_NIGHT_YES
        setTheme(if (initialMode.isDark(systemDark)) R.style.Theme_Feiniu_Dark else R.style.Theme_Feiniu_Light)
        super.onCreate(savedInstanceState)
        sharedText = incomingText(intent)
        setContent {
            var themeMode by remember { mutableStateOf(initialMode) }
            val darkTheme = themeMode.isDark(isSystemInDarkTheme())
            FeiniuTheme(darkTheme = darkTheme) {
                val background = MaterialTheme.colorScheme.background.toArgb()
                LaunchedEffect(darkTheme) {
                    setTheme(if (darkTheme) R.style.Theme_Feiniu_Dark else R.style.Theme_Feiniu_Light)
                    window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(background))
                    val bars = if (darkTheme) SystemBarStyle.dark(android.graphics.Color.TRANSPARENT)
                        else SystemBarStyle.light(android.graphics.Color.TRANSPARENT, android.graphics.Color.TRANSPARENT)
                    enableEdgeToEdge(statusBarStyle = bars, navigationBarStyle = bars)
                    updateBarAppearance(darkTheme)
                }
                FeiniuApp(
                    AppRepository.get(applicationContext), sharedText,
                    themeMode = themeMode,
                    changeTheme = { mode -> appearance.writeTheme(mode); themeMode = mode },
                    consumeSharedText = { sharedText = "" },
                )
            }
        }
    }

    private fun updateBarAppearance(dark: Boolean) {
        WindowCompat.getInsetsController(window, window.decorView).apply {
            isAppearanceLightStatusBars = !dark
            isAppearanceLightNavigationBars = !dark
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) {
            val systemDark = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK == Configuration.UI_MODE_NIGHT_YES
            updateBarAppearance(AppearanceSettings(this).readTheme().isDark(systemDark))
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        sharedText = incomingText(intent)
    }

    private fun incomingText(intent: Intent?): String = when (intent?.action) {
        Intent.ACTION_SEND -> intent.getStringExtra(Intent.EXTRA_TEXT).orEmpty()
        Intent.ACTION_VIEW -> intent.dataString.orEmpty()
        else -> ""
    }
}

private enum class HomeTab(val label: String, val icon: ImageVector) {
    TASKS("首页", Icons.Outlined.Home),
    NAS("家庭影库", Icons.Outlined.Tv),
    LIBRARY("我的下载", Icons.Outlined.VideoLibrary),
    SETTINGS("设置", Icons.Outlined.Settings),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FeiniuApp(
    repository: AppRepository, sharedText: String, themeMode: ThemeMode,
    changeTheme: (ThemeMode) -> Unit, consumeSharedText: () -> Unit,
) {
    val context = LocalContext.current
    val tasks by repository.tasks.collectAsState()
    val scope = rememberCoroutineScope()
    val snackbar = remember { SnackbarHostState() }
    var tab by rememberSaveable { mutableIntStateOf(0) }
    var link by rememberSaveable { mutableStateOf("") }
    var playingId by rememberSaveable { mutableStateOf<String?>(null) }
    var nasPlayback by remember { mutableStateOf<NasPlayback?>(null) }
    var pendingExportId by rememberSaveable { mutableStateOf<String?>(null) }
    var exporting by remember { mutableStateOf(false) }
    var managingFiles by rememberSaveable { mutableStateOf(false) }
    var cleanupTaskId by rememberSaveable { mutableStateOf<String?>(null) }
    val notify: (String) -> Unit = { message -> scope.launch { snackbar.showSnackbar(message, withDismissAction = true) } }

    val notificationPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    val saveDiagnostics = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("text/plain")) { uri ->
        if (uri != null) scope.launch {
            runCatching { writeDiagnostics(context, uri, repository.diagnostics() + "\n\n" + playbackDiagnostics(context)) }
                .onSuccess { notify("诊断已保存，可附给开发者排查。") }
                .onFailure { notify(it.message ?: "诊断保存失败") }
        }
    }
    val saveFolder = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        val taskId = pendingExportId
        pendingExportId = null
        if (uri != null && taskId != null) scope.launch {
            exporting = true
            try {
                val count = repository.exportTask(taskId, uri)
                notify("已保存 $count 个文件到新的视频文件夹。")
            } catch (error: Exception) {
                notify(error.message ?: "保存失败，请检查文件夹权限和剩余空间。")
            } finally {
                exporting = false
            }
        }
    }
    LaunchedEffect(sharedText) {
        if (sharedText.isNotBlank()) {
            link = extractLink(sharedText)
            playingId = null
            nasPlayback = null
            tab = 0
            consumeSharedText()
        }
    }

    nasPlayback?.let { playback ->
        NasPlayerScreen(playback, onBack = { nasPlayback = null })
        return
    }

    val playing = tasks.firstOrNull { it.id == playingId }
    if (playing != null) {
        PlayerScreen(playing, onBack = { playingId = null })
        return
    }

    val export: (DownloadTask) -> Unit = {
        pendingExportId = it.id
        saveFolder.launch(null)
    }
    val act: (() -> Unit) -> Unit = { action ->
        runCatching(action).onFailure { notify(it.message ?: "操作失败，请重试。") }
    }

    MaterialAppScaffold(
        destinations = HomeTab.entries.map { MaterialDestination(it.label, it.icon) },
        selected = tab, select = { tab = it },
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Surface(color = ReadingColors.CaptionBlue, shape = MaterialTheme.shapes.small) {
                            Icon(Icons.Outlined.Subtitles, contentDescription = null, tint = MaterialTheme.colorScheme.onPrimary, modifier = Modifier.padding(8.dp).size(22.dp))
                        }
                        Text("飞牛字幕", style = MaterialTheme.typography.titleLarge)
                    }
                },
                actions = {
                    if (HomeTab.entries[tab] != HomeTab.SETTINGS) IconButton(onClick = { tab = HomeTab.SETTINGS.ordinal }) {
                        Icon(Icons.Outlined.Settings, contentDescription = "打开设置")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = ReadingColors.Paper),
            )
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize(), contentAlignment = Alignment.TopCenter) {
            Column(Modifier.widthIn(max = if (HomeTab.entries[tab] == HomeTab.SETTINGS) 760.dp else 1440.dp)
                .fillMaxHeight().fillMaxWidth()) {
                if (exporting) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text("正在把视频和字幕保存到你选择的文件夹…", modifier = Modifier.padding(horizontal = 20.dp, vertical = 8.dp), style = MaterialTheme.typography.bodySmall)
                }
                when (HomeTab.entries[tab]) {
                    HomeTab.TASKS -> TaskList(
                        tasks.filterNot { it.historyHidden }, link, { link = it },
                        submit = {
                            act {
                                repository.submit(extractLink(link))
                                link = ""
                                if (Build.VERSION.SDK_INT >= 33) notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                                notify("已开始准备，完成后会出现在“我的下载”。")
                            }
                        },
                        repository = repository,
                        play = { playingId = it.id }, export = export, exportEnabled = !exporting,
                        cleanup = { cleanupTaskId = it.id },
                        act = act,
                    )
                    HomeTab.NAS -> NasLibraryScreen(
                        settings = repository.settings(),
                        onSettings = { tab = HomeTab.SETTINGS.ordinal },
                        onPlay = { nasPlayback = it },
                        onDownload = { playback ->
                            act {
                                repository.submitNasMedia(playback)
                                if (Build.VERSION.SDK_INT >= 33) notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                                notify("已开始保存到手机，可在首页查看进度。")
                            }
                        },
                        notify = notify,
                    )
                    HomeTab.LIBRARY -> LibraryList(tasks, { playingId = it.id }, export, !exporting,
                        cleanup = { cleanupTaskId = it.id }, add = { tab = 0 })
                    HomeTab.SETTINGS -> if (managingFiles) {
                        FileCleanupScreen(repository, enabled = !exporting, onBack = { managingFiles = false })
                    } else ConnectionScreen(repository, themeMode, changeTheme,
                        manageFiles = { managingFiles = true },
                        exportDiagnostics = {
                            val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
                            saveDiagnostics.launch("飞牛字幕-诊断-$stamp.txt")
                        },
                    )
                }
            }
        }
    }
    tasks.firstOrNull { it.id == cleanupTaskId }?.let { task ->
        TaskCleanupDialog(task, repository, enabled = !exporting, onDismiss = { cleanupTaskId = null }, notify = notify)
    }
}

@Composable
@OptIn(ExperimentalLayoutApi::class)
private fun TaskList(
    tasks: List<DownloadTask>, link: String, changeLink: (String) -> Unit, submit: () -> Unit,
    repository: AppRepository, play: (DownloadTask) -> Unit, export: (DownloadTask) -> Unit,
    cleanup: (DownloadTask) -> Unit,
    exportEnabled: Boolean, act: (() -> Unit) -> Unit,
) {
    @Suppress("DEPRECATION") val clipboard = LocalClipboardManager.current
    var filter by rememberSaveable { mutableStateOf(TaskFilter.ALL) }
    var query by rememberSaveable { mutableStateOf("") }
    val visible = remember(tasks, filter, query) { filterTasks(tasks, filter, query) }
    val listState = rememberLazyListState()
    MaterialTaskLayout(tasks = tasks, detail = { task ->
        TaskPanel(task, play, export, exportEnabled, cleanup,
            retryVideo = { act { repository.retryVideo(task.id) } },
            retrySubtitle = { act { repository.retrySubtitles(task.id) } },
            cancelVideo = { act { repository.cancelVideo(task.id) } },
            cancelSubtitle = { act { repository.cancelSubtitles(task.id) } },
        )
    }) { selectedId, select ->
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize().imePadding(),
            contentPadding = PaddingValues(horizontal = 20.dp, vertical = 16.dp),
            verticalArrangement = Arrangement.spacedBy(18.dp),
        ) {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("今天想看什么？", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
                    Text("粘贴视频链接，我们会帮你下载视频并准备中文字幕。", style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
                }
            }
            item {
                MaterialPanel() {
                    Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                        OutlinedTextField(
                            value = link, onValueChange = changeLink,
                            modifier = Modifier.fillMaxWidth(), label = { Text("视频链接") },
                            placeholder = { Text("粘贴 YouTube 视频链接") },
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                            maxLines = 3, shape = MaterialTheme.shapes.small,
                        )
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedButton(onClick = { changeLink(extractLink(clipboard.getText()?.text.orEmpty())) }, modifier = Modifier.heightIn(min = 52.dp)) {
                                Icon(Icons.Outlined.ContentPaste, contentDescription = null, modifier = Modifier.size(18.dp))
                                Spacer(Modifier.width(8.dp)); Text("粘贴")
                            }
                            Button(onClick = submit, enabled = link.isNotBlank(), modifier = Modifier.weight(1f).heightIn(min = 52.dp)) {
                                Icon(Icons.Outlined.Add, contentDescription = null, modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(8.dp)); Text("开始准备")
                            }
                        }
                    }
                }
            }
            item {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("最近添加", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f).semantics { heading() })
                    Text("${tasks.size} 条", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                }
            }
            if (tasks.isNotEmpty()) item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        value = query, onValueChange = { query = it },
                        label = { Text("搜索视频") }, singleLine = true,
                        leadingIcon = { Icon(Icons.Outlined.Search, contentDescription = null) },
                        trailingIcon = { if (query.isNotEmpty()) IconButton(onClick = { query = "" }) {
                            Icon(Icons.Outlined.Close, contentDescription = "清除搜索")
                        } },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TaskFilter.entries.forEach { choice ->
                            val count = if (choice == TaskFilter.ALL) tasks.size else tasks.count { it.category() == choice }
                            MaterialFilterChip(selected = filter == choice, onClick = { filter = choice },
                                label = { Text("${choice.label} $count") }, modifier = Modifier.heightIn(min = 48.dp))
                        }
                    }
                }
            }
            if (tasks.isNotEmpty() && visible.isEmpty()) item {
                EmptyCollection(Icons.Outlined.SearchOff, "没有找到相关视频", "换个关键词试试，或切换到“全部”。")
            }
            if (tasks.isEmpty()) item {
                EmptyCollection(
                    icon = Icons.Outlined.Subtitles,
                    title = "还没有视频",
                    body = "复制一条视频链接后回到这里，或在视频 App 里点“分享”并选择飞牛字幕。",
                )
            }
            items(visible, key = { it.id }) { task ->
                if (select != null) MaterialTaskSummary(task, selectedId == task.id) { select(task.id) }
                else TaskPanel(task, play, export, exportEnabled, cleanup,
                    retryVideo = { act { repository.retryVideo(task.id) } },
                    retrySubtitle = { act { repository.retrySubtitles(task.id) } },
                    cancelVideo = { act { repository.cancelVideo(task.id) } },
                    cancelSubtitle = { act { repository.cancelSubtitles(task.id) } },
                )
            }
            item { Spacer(Modifier.height(8.dp)) }
        }
    }
}

@Composable
@OptIn(ExperimentalLayoutApi::class)
private fun TaskPanel(
    task: DownloadTask, play: (DownloadTask) -> Unit, export: (DownloadTask) -> Unit, exportEnabled: Boolean,
    cleanup: (DownloadTask) -> Unit,
    retryVideo: () -> Unit, retrySubtitle: () -> Unit, cancelVideo: () -> Unit, cancelSubtitle: () -> Unit,
) {
    MaterialPanel() {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(task.title.ifBlank { "正在读取视频信息" }, style = MaterialTheme.typography.titleMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
                Text(formatDate(task.createdAt), style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
            }
            val cleaning = task.localCleanupPending || task.remoteCleanupPending
            if (cleaning || task.localFilesDeleted || task.remoteFilesDeleted) {
                Text(cleanupStatus(task), style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
            }
            if (!task.cleanupError.isNullOrBlank()) {
                Text(task.cleanupError, style = MaterialTheme.typography.bodySmall, color = ReadingColors.Error)
            }
            TransferTrack("视频", Icons.Outlined.Smartphone, task.videoState, task.videoProgress, task.videoError, ReadingColors.CaptionBlue, retryVideo, cancelVideo,
                actionsEnabled = !cleaning, cleared = task.localFilesDeleted)
            HorizontalDivider(color = ReadingColors.Border.copy(alpha = 0.55f))
            TransferTrack("中文字幕", Icons.Outlined.Subtitles, task.subtitleState, task.subtitleProgress, task.subtitleError, ReadingColors.LocalTeal, retrySubtitle, cancelSubtitle,
                actionsEnabled = !cleaning, cleared = task.localFilesDeleted && task.originalPath == null)
            val localVideo = task.videoPath?.let { File(it).isFile } == true
            val anyFiles = localVideo || listOf(task.originalPath, task.zhPath, task.bilingualPath).any { it?.let(::File)?.isFile == true }
            if (anyFiles && !task.localCleanupPending) FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                if (localVideo) Button(onClick = { play(task) }, modifier = Modifier.heightIn(min = 48.dp)) {
                    Icon(Icons.Outlined.PlayArrow, contentDescription = null, modifier = Modifier.size(20.dp)); Spacer(Modifier.width(4.dp)); Text("播放")
                }
                OutlinedButton(onClick = { export(task) }, enabled = exportEnabled, modifier = Modifier.heightIn(min = 48.dp)) {
                    Icon(Icons.Outlined.FolderOpen, contentDescription = null, modifier = Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)); Text("另存为")
                }
            }
            TextButton(onClick = { cleanup(task) }, enabled = exportEnabled, modifier = Modifier.align(Alignment.End).heightIn(min = 48.dp)) {
                Icon(Icons.Outlined.DeleteOutline, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(6.dp)); Text(if (cleaning) "查看删除进度" else "删除文件")
            }
        }
    }
}

@Composable
private fun TransferTrack(
    label: String, icon: ImageVector, state: TaskState, progress: Float, error: String?, color: Color,
    retry: () -> Unit, cancel: () -> Unit,
    actionsEnabled: Boolean = true, cleared: Boolean = false,
) {
    val isActive = state in listOf(TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING)
    val stateColor = when (state) {
        TaskState.FAILED -> ReadingColors.Error
        TaskState.WAITING -> ReadingColors.Waiting
        TaskState.READY -> ReadingColors.LocalTeal
        else -> ReadingColors.SecondaryInk
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Icon(icon, contentDescription = null, tint = color, modifier = Modifier.size(18.dp))
            Text(label, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
            if (state == TaskState.READY) Icon(Icons.Outlined.CheckCircle, contentDescription = "已完成", tint = stateColor, modifier = Modifier.size(18.dp))
            Text(if (cleared) "已删除" else stateLabel(state, progress), style = MaterialTheme.typography.bodySmall, color = stateColor)
        }
        if (state == TaskState.RUNNING && progress <= 0f) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth().height(4.dp), color = color, trackColor = ReadingColors.Inset)
        } else if (isActive) {
            LinearProgressIndicator(progress = { progress.coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth().height(4.dp), color = color, trackColor = ReadingColors.Inset)
        }
        if (!error.isNullOrBlank() && state != TaskState.READY) {
            Text(error.take(500), style = MaterialTheme.typography.bodySmall, color = stateColor)
        }
        if (state == TaskState.WAITING && error.isNullOrBlank()) {
            Text("网络恢复后会自动继续，进度不会丢失。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
        }
        if (isActive || state == TaskState.FAILED || state == TaskState.CANCELLED) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                if (state in listOf(TaskState.FAILED, TaskState.CANCELLED, TaskState.WAITING)) {
                    TextButton(onClick = retry, enabled = actionsEnabled, modifier = Modifier.heightIn(min = 48.dp)) {
                        Icon(Icons.Outlined.Refresh, contentDescription = null, modifier = Modifier.size(18.dp)); Spacer(Modifier.width(4.dp)); Text(if (cleared) "重新下载" else if (state == TaskState.CANCELLED) "继续" else "重试")
                    }
                }
                if (isActive) TextButton(onClick = cancel, enabled = actionsEnabled, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消", color = ReadingColors.SecondaryInk) }
            }
        }
    }
}

@Composable
@OptIn(ExperimentalLayoutApi::class)
private fun LibraryList(tasks: List<DownloadTask>, play: (DownloadTask) -> Unit, export: (DownloadTask) -> Unit, exportEnabled: Boolean,
    cleanup: (DownloadTask) -> Unit, add: () -> Unit) {
    val available = tasks.filter { !it.localCleanupPending && it.videoPath?.let(::File)?.isFile == true }
    LazyColumn(contentPadding = PaddingValues(horizontal = 20.dp, vertical = 16.dp), verticalArrangement = Arrangement.spacedBy(18.dp), modifier = Modifier.fillMaxSize()) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("我的下载", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
                Text("${available.size} 个视频已保存在手机上，没网也能看。", color = ReadingColors.SecondaryInk, style = MaterialTheme.typography.bodyMedium)
            }
        }
        if (available.isEmpty()) item {
            EmptyCollection(Icons.Outlined.OfflinePin, "这里还空着", "下载完成的视频会出现在这里，出门没网也能继续看。")
            Button(onClick = add, modifier = Modifier.fillMaxWidth().padding(top = 16.dp).heightIn(min = 48.dp)) { Text("去添加视频") }
        }
        items(available, key = { it.id }) { task ->
            MaterialPanel() {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
                        Surface(color = ReadingColors.BlueWash, shape = MaterialTheme.shapes.medium) {
                            Icon(Icons.Outlined.Movie, contentDescription = null, tint = ReadingColors.CaptionBlue, modifier = Modifier.padding(16.dp).size(24.dp))
                        }
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text(task.title, style = MaterialTheme.typography.titleMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
                            Text(localFileLabel(task), style = MaterialTheme.typography.bodySmall, color = ReadingColors.LocalTeal)
                        }
                    }
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Button(onClick = { play(task) }, modifier = Modifier.heightIn(min = 48.dp)) {
                            Icon(Icons.Outlined.PlayArrow, contentDescription = null); Spacer(Modifier.width(4.dp)); Text("播放")
                        }
                        OutlinedButton(onClick = { export(task) }, enabled = exportEnabled, modifier = Modifier.heightIn(min = 48.dp)) { Text("另存为") }
                    }
                    TextButton(onClick = { cleanup(task) }, enabled = exportEnabled, modifier = Modifier.align(Alignment.End).heightIn(min = 48.dp)) {
                        Icon(Icons.Outlined.DeleteOutline, contentDescription = null, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(6.dp)); Text(if (task.remoteCleanupPending) "查看删除进度" else "删除文件")
                    }
                }
            }
        }
        if (available.isNotEmpty()) item {
            Text("想在其他播放器中打开或长期保存？点“另存为”，视频和字幕会一起保存到你选择的文件夹。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
        }
    }
}

@Composable
private fun ConnectionScreen(
    repository: AppRepository, themeMode: ThemeMode, changeTheme: (ThemeMode) -> Unit,
    manageFiles: () -> Unit,
    exportDiagnostics: () -> Unit,
) {
    val context = LocalContext.current
    val saved = remember { repository.settings() }
    var address by rememberSaveable { mutableStateOf(saved.baseUrl) }
    var token by remember { mutableStateOf(saved.token) }
    var proxy by rememberSaveable { mutableStateOf(saved.downloadProxy) }
    var tokenVisible by remember { mutableStateOf(false) }
    var advanced by rememberSaveable { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var result by remember { mutableStateOf<String?>(null) }
    var failed by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val save: () -> Boolean = {
        try {
            repository.saveSettings(ConnectionSettings(address.trim(), token.trim(), proxy.trim()))
            result = "设置已保存。"
            failed = false
            true
        } catch (error: Exception) {
            result = error.message ?: "保存失败，请检查填写的内容。"
            failed = true
            false
        }
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding().padding(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        FriendlyPageHeader("设置", "连接家里的飞牛，并调整 App 的使用偏好。")
        MaterialPanel() {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                SettingsHeading(Icons.Outlined.Home, "连接家里的飞牛", "连接后可以准备字幕，也能浏览家庭影库。")
                OutlinedTextField(
                    value = address, onValueChange = { address = it; result = null },
                    label = { Text("飞牛地址") }, placeholder = { Text("例如 https://my-nas.example.com") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                    supportingText = { Text("请填写在手机上能打开的地址。") },
                    singleLine = true, modifier = Modifier.fillMaxWidth(), enabled = !busy,
                )
                OutlinedTextField(
                    value = token, onValueChange = { token = it; result = null }, label = { Text("连接密码") },
                    visualTransformation = if (tokenVisible) VisualTransformation.None else PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    trailingIcon = {
                        IconButton(onClick = { tokenVisible = !tokenVisible }) {
                            Icon(if (tokenVisible) Icons.Outlined.VisibilityOff else Icons.Outlined.Visibility, contentDescription = if (tokenVisible) "隐藏连接密码" else "显示连接密码")
                        }
                    },
                    supportingText = { Text("可在飞牛字幕服务中查看，保存后会加密存储。") },
                    singleLine = true, modifier = Modifier.fillMaxWidth(), enabled = !busy,
                )
                Button(
                    onClick = {
                        if (save()) scope.launch {
                            busy = true
                            result = null
                            try { result = repository.testConnection(); failed = false }
                            catch (error: Exception) { result = error.message ?: "暂时连不上家里的飞牛，请检查地址和网络。"; failed = true }
                            finally { busy = false }
                        }
                    },
                    enabled = !busy && address.isNotBlank(), modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp),
                ) {
                    if (busy) {
                        CircularProgressIndicator(Modifier.size(18.dp), color = MaterialTheme.colorScheme.onPrimary, strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                    }
                    Text(if (busy) "正在连接…" else "保存并连接")
                }
                if (result != null) Surface(color = if (failed) ReadingColors.ErrorWash else ReadingColors.TealWash, shape = MaterialTheme.shapes.small) {
                    Text(result.orEmpty(), modifier = Modifier.fillMaxWidth().padding(14.dp), style = MaterialTheme.typography.bodyMedium, color = if (failed) ReadingColors.Error else ReadingColors.LocalTeal)
                }
            }
        }
        AppearanceCard(themeMode, changeTheme)
        MaterialPanel() {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                SettingsHeading(Icons.Outlined.Storage, "存储空间", "删除不再需要的视频和字幕，释放手机空间。")
                OutlinedButton(onClick = manageFiles, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Icon(Icons.Outlined.DeleteOutline, contentDescription = null, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp)); Text("管理已下载内容")
                }
            }
        }
        MaterialPanel() {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                TextButton(onClick = { advanced = !advanced }, contentPadding = PaddingValues(0.dp), modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)) {
                    Icon(Icons.Outlined.Tune, contentDescription = null, tint = ReadingColors.CaptionBlue)
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        Text("高级设置", style = MaterialTheme.typography.titleMedium, color = ReadingColors.Ink)
                        Text("网络、后台运行和问题反馈", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                    }
                    Icon(if (advanced) Icons.Outlined.ExpandLess else Icons.Outlined.ExpandMore, contentDescription = if (advanced) "收起高级设置" else "展开高级设置")
                }
                if (advanced) {
                    HorizontalDivider(color = ReadingColors.Border)
                    Text("网络代理", style = MaterialTheme.typography.titleSmall)
                    OutlinedTextField(
                        value = proxy, onValueChange = { proxy = it; result = null },
                        label = { Text("代理地址（可选）") }, placeholder = { Text("例如 http://127.0.0.1:7890") },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                        supportingText = { Text("通常不需要填写。只有视频无法下载、且你知道代理地址时再使用。") },
                        modifier = Modifier.fillMaxWidth(), singleLine = true, enabled = !busy,
                    )
                    OutlinedButton(onClick = { save() }, enabled = !busy, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("保存网络设置") }
                    HorizontalDivider(color = ReadingColors.Border)
                    Text("后台下载", style = MaterialTheme.typography.titleSmall)
                    Text("如果锁屏后下载暂停，可以在系统设置中允许飞牛字幕后台运行。", color = ReadingColors.SecondaryInk, style = MaterialTheme.typography.bodyMedium)
                    OutlinedButton(
                        onClick = {
                            runCatching {
                                context.startActivity(Intent(
                                    android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                    android.net.Uri.parse("package:${context.packageName}"),
                                ))
                            }.onFailure {
                                result = "无法自动打开，请在手机设置中找到“飞牛字幕”。"
                                failed = true
                            }
                        },
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                    ) {
                        Icon(Icons.AutoMirrored.Outlined.OpenInNew, contentDescription = null, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(8.dp)); Text("打开系统设置")
                    }
                    HorizontalDivider(color = ReadingColors.Border)
                    Text("遇到问题", style = MaterialTheme.typography.titleSmall)
                    Text("可以生成一份问题报告，方便排查下载或连接问题。", color = ReadingColors.SecondaryInk, style = MaterialTheme.typography.bodyMedium)
                    OutlinedButton(onClick = exportDiagnostics, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                        Icon(Icons.Outlined.Description, contentDescription = null, modifier = Modifier.size(20.dp)); Spacer(Modifier.width(8.dp)); Text("保存问题报告")
                    }
                }
            }
        }
        Text("卸载 App 前，请先把需要保留的视频“另存为”到手机文件夹。", color = ReadingColors.SecondaryInk, style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(horizontal = 4.dp))
    }
}

@Composable
private fun SettingsHeading(icon: ImageVector, title: String, description: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Surface(color = ReadingColors.BlueWash, shape = MaterialTheme.shapes.small) {
            Icon(icon, contentDescription = null, tint = ReadingColors.CaptionBlue, modifier = Modifier.padding(10.dp).size(22.dp))
        }
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.semantics { heading() })
            Text(description, style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
        }
    }
}

@Composable
private fun AppearanceCard(themeMode: ThemeMode, changeTheme: (ThemeMode) -> Unit) {
    MaterialPanel() {
        Column(Modifier.fillMaxWidth().padding(20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            SettingsHeading(Icons.Outlined.Palette, "外观", "选择你喜欢的显示方式。")
            Column(Modifier.fillMaxWidth().selectableGroup()) {
                ThemeMode.entries.forEach { mode ->
                    Row(
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                            .selectable(selected = themeMode == mode, onClick = { changeTheme(mode) }, role = Role.RadioButton),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        RadioButton(selected = themeMode == mode, onClick = null)
                        Text(mode.label, style = MaterialTheme.typography.bodyLarge)
                    }
                }
            }
        }
    }
}

@Composable
private fun EmptyCollection(icon: ImageVector, title: String, body: String) {
    Surface(color = ReadingColors.Inset, shape = MaterialTheme.shapes.large) {
        Column(Modifier.fillMaxWidth().padding(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Icon(icon, contentDescription = null, tint = ReadingColors.CaptionBlue, modifier = Modifier.size(32.dp))
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(body, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
        }
    }
}

private fun extractLink(text: String): String = Regex("https?://[^\\s<>]+", RegexOption.IGNORE_CASE)
    .find(text)?.value?.trimEnd('.', ',', '，', '。', ')', '）') ?: text.trim()

internal fun stateLabel(state: TaskState, progress: Float): String = when (state) {
    TaskState.QUEUED -> "即将开始"
    TaskState.RUNNING -> if (progress > 0f) "${(progress.coerceIn(0f, 1f) * 100).toInt()}%" else "准备中"
    TaskState.WAITING -> "稍后继续"
    TaskState.READY -> "已完成"
    TaskState.FAILED -> "未完成"
    TaskState.CANCELLED -> "已取消"
    TaskState.SKIPPED -> "暂无字幕"
}

private fun formatDate(time: Long): String = SimpleDateFormat("M 月 d 日 HH:mm", Locale.CHINA).format(Date(time))

private fun localFileLabel(task: DownloadTask): String {
    val size = task.videoPath?.let(::File)?.length() ?: 0L
    val sizeText = if (size >= 1_073_741_824L) String.format(Locale.CHINA, "%.1f GB", size / 1_073_741_824.0) else String.format(Locale.CHINA, "%.0f MB", size / 1_048_576.0)
    val subtitle = when {
        task.bilingualPath?.let(::File)?.isFile == true -> "中英字幕可用"
        task.zhPath?.let(::File)?.isFile == true -> "中文字幕可用"
        task.originalPath?.let(::File)?.isFile == true -> "原语言字幕可用"
        else -> "视频可离线播放"
    }
    return "$sizeText · $subtitle"
}

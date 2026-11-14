package cn.qwensub.app

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.ActivityInfo
import android.net.Uri
import android.view.ViewGroup
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.CaptionStyleCompat
import androidx.media3.ui.PlayerView
import java.io.File
import java.net.Proxy
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

private data class SubtitleChoice(val id: String, val label: String, val path: String?, val language: String?, val remote: Boolean = false) {
    val available: Boolean get() = id == "off" || remote || path.existsLocally()
}

@OptIn(ExperimentalMaterial3Api::class)
@androidx.annotation.OptIn(UnstableApi::class)
@Composable
fun PlayerScreen(
    task: DownloadTask, onBack: () -> Unit, nas: NasPlayback? = null,
    subtitleActions: (@Composable () -> Unit)? = null,
    onNasSubtitleSelected: ((String?) -> Unit)? = null,
    playbackActions: (@Composable () -> Unit)? = null,
) {
    val context = LocalContext.current
    val activity = context.activity()
    val darkTheme = MaterialTheme.colorScheme.background.luminance() < 0.5f
    val currentDarkTheme by rememberUpdatedState(darkTheme)
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val preferences = remember { context.getSharedPreferences("playback", Context.MODE_PRIVATE) }
    var position by rememberSaveable(task.id) { mutableLongStateOf(preferences.getLong("position_${task.id}", 0)) }
    var fullscreen by rememberSaveable(task.id) { mutableStateOf(false) }
    val currentFullscreen by rememberUpdatedState(fullscreen)
    var subtitleMode by rememberSaveable(task.id) {
        mutableStateOf(when {
            nas?.selectedSubtitleId != null -> nas.selectedSubtitleId
            task.bilingualPath.existsLocally() -> "bilingual"
            task.zhPath.existsLocally() -> "zh"
            task.originalPath.existsLocally() -> "original"
            else -> "off"
        })
    }
    var speed by rememberSaveable(task.id) { mutableFloatStateOf(1f) }
    var error by remember(task.id) { mutableStateOf<String?>(null) }
    var playing by remember(task.id) { mutableStateOf(false) }
    var buffering by remember(task.id) { mutableStateOf(true) }
    var controlsVisible by remember(task.id) { mutableStateOf(true) }
    val selectSubtitle: (String) -> Unit = { id ->
        subtitleMode = id
        onNasSubtitleSelected?.invoke(id.takeUnless { it == "off" })
    }
    val choices = if (nas != null) nas.subtitles.map {
        SubtitleChoice(it.id, it.title, MediaClient(nas.settings).resolvePath(it.downloadPath), it.language, remote = true)
    } + SubtitleChoice("off", "关闭", null, null) else listOf(
        SubtitleChoice("bilingual", "中英双语", task.bilingualPath, "zh"),
        SubtitleChoice("zh", "中文", task.zhPath, "zh"),
        SubtitleChoice("original", "原语言", task.originalPath, "und"),
        SubtitleChoice("off", "关闭", null, null),
    )
    val selected = choices.firstOrNull { it.id == subtitleMode } ?: choices.last()
    LaunchedEffect(nas?.selectedSubtitleId) {
        if (nas?.selectedSubtitleId != null) subtitleMode = nas.selectedSubtitleId
    }
    val engine = remember(task.id, nas?.settings) {
        val sourceFactory = if (nas != null) {
            val http = OkHttpClient.Builder().proxy(Proxy.NO_PROXY)
                .connectTimeout(20, TimeUnit.SECONDS).readTimeout(90, TimeUnit.SECONDS)
                .followRedirects(false).followSslRedirects(false).build()
            val dataSource = OkHttpDataSource.Factory(http)
                .setDefaultRequestProperties(mapOf("Authorization" to "Bearer ${nas.settings.token}"))
            DefaultMediaSourceFactory(DefaultDataSource.Factory(context, dataSource))
        } else DefaultMediaSourceFactory(context)
        ExoPlayer.Builder(context)
            .setMediaSourceFactory(sourceFactory.setSubtitleParserFactory(PlaybackSubtitleParserFactory()))
            .setSeekBackIncrementMs(10_000).setSeekForwardIncrementMs(10_000).build()
    }

    // Persist at every lifecycle boundary, including a process being removed in the background.
    fun savePosition() {
        position = if (engine.playbackState == Player.STATE_ENDED) 0 else engine.currentPosition.coerceAtLeast(0)
        preferences.edit().putLong("position_${task.id}", position).apply()
    }

    DisposableEffect(engine, lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_PAUSE || event == Lifecycle.Event.ON_STOP) {
                savePosition()
                engine.pause()
            }
        }
        val listener = object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) { playing = isPlaying }
            override fun onPlaybackStateChanged(playbackState: Int) {
                buffering = playbackState == Player.STATE_BUFFERING
                if (playbackState == Player.STATE_ENDED) preferences.edit().putLong("position_${task.id}", 0).apply()
            }
            override fun onPlayerError(playbackError: PlaybackException) {
                error = "视频暂时无法播放，请检查网络后重试。"
                preferences.edit()
                    .putString("last_task", task.id)
                    .putString("last_error_code", playbackError.errorCodeName)
                    .putLong("last_error_at", System.currentTimeMillis())
                    .apply()
                buffering = false
            }
        }
        lifecycle.addObserver(observer)
        engine.addListener(listener)
        onDispose {
            savePosition()
            lifecycle.removeObserver(observer)
            engine.removeListener(listener)
            engine.release()
        }
    }

    LaunchedEffect(engine, task.videoPath, nas?.file?.streamPath, selected.id, selected.path) {
        val file = task.videoPath?.let(::File)
        if (nas == null && file?.isFile != true) {
            error = "视频文件已不存在，请返回任务列表重新下载。"
            buffering = false
            return@LaunchedEffect
        }
        val resumeAt = if (engine.mediaItemCount == 0) position else engine.currentPosition
        val resumePlaying = if (engine.mediaItemCount == 0) true else engine.playWhenReady
        val subtitles = selected.path?.takeIf { selected.available }?.let { path ->
            listOf(MediaItem.SubtitleConfiguration.Builder(if (selected.remote) Uri.parse(path) else Uri.fromFile(File(path)))
                .setId(selected.id)
                .setLabel(selected.label)
                .setMimeType(MimeTypes.APPLICATION_SUBRIP)
                .setLanguage(selected.language)
                .setSelectionFlags(C.SELECTION_FLAG_DEFAULT)
                .build())
        }.orEmpty()
        val media = MediaItem.Builder()
            .setMediaId(task.id)
            .setUri(if (nas != null) Uri.parse(MediaClient(nas.settings).resolvePath(nas.file.streamPath)) else Uri.fromFile(requireNotNull(file)))
            .setSubtitleConfigurations(subtitles)
            .build()
        error = null
        engine.trackSelectionParameters = engine.trackSelectionParameters.buildUpon()
            .setTrackTypeDisabled(C.TRACK_TYPE_TEXT, selected.id == "off")
            .setPreferredTextLanguage(selected.language)
            .build()
        engine.setMediaItem(media, resumeAt.coerceAtLeast(0))
        engine.setPlaybackSpeed(speed)
        engine.prepare()
        engine.playWhenReady = resumePlaying
    }
    LaunchedEffect(speed) { engine.setPlaybackSpeed(speed) }

    DisposableEffect(activity) {
        val originalOrientation = activity?.requestedOrientation ?: ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        onDispose {
            activity?.takeUnless { it.isChangingConfigurations }?.let {
                it.requestedOrientation = originalOrientation
                WindowCompat.getInsetsController(it.window, it.window.decorView).apply {
                    isAppearanceLightStatusBars = !currentDarkTheme
                    isAppearanceLightNavigationBars = !currentDarkTheme
                    show(WindowInsetsCompat.Type.systemBars())
                }
            }
        }
    }
    LaunchedEffect(activity, fullscreen, darkTheme) {
        if (activity != null) {
            val controls = WindowCompat.getInsetsController(activity.window, activity.window.decorView)
            controls.isAppearanceLightStatusBars = !fullscreen && !darkTheme
            controls.isAppearanceLightNavigationBars = !fullscreen && !darkTheme
            if (fullscreen) {
                activity.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                controls.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                controls.hide(WindowInsetsCompat.Type.systemBars())
            } else {
                activity.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
                controls.show(WindowInsetsCompat.Type.systemBars())
            }
        }
    }
    BackHandler { if (fullscreen) fullscreen = false else onBack() }

    val video: @Composable (Modifier) -> Unit = { modifier ->
        Box(modifier.background(Color.Black)) {
            AndroidView(
                factory = { viewContext ->
                    PlayerView(viewContext).apply {
                        player = engine
                        layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                        resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
                        useController = true
                        setShowRewindButton(true)
                        setShowFastForwardButton(true)
                        setShowPreviousButton(false)
                        setShowNextButton(false)
                        setShowSubtitleButton(false)
                        controllerShowTimeoutMs = 4_000
                        subtitleView?.setApplyEmbeddedStyles(false)
                        subtitleView?.setStyle(CaptionStyleCompat(
                            android.graphics.Color.WHITE,
                            0x80000000.toInt(),
                            android.graphics.Color.TRANSPARENT,
                            CaptionStyleCompat.EDGE_TYPE_OUTLINE,
                            android.graphics.Color.BLACK,
                            android.graphics.Typeface.DEFAULT,
                        ))
                        controlsVisible = isControllerFullyVisible
                        addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> updateSubtitleLayout(currentFullscreen, controlsVisible) }
                        setControllerVisibilityListener(PlayerView.ControllerVisibilityListener { visibility ->
                            controlsVisible = visibility == android.view.View.VISIBLE
                            updateSubtitleLayout(currentFullscreen, controlsVisible)
                        })
                    }
                },
                update = { view ->
                    view.player = engine
                    view.keepScreenOn = playing
                    view.updateSubtitleLayout(fullscreen, controlsVisible)
                },
                onReset = null,
                onRelease = { it.player = null },
                modifier = Modifier.fillMaxSize(),
            )
            if (buffering && error == null) CircularProgressIndicator(
                modifier = Modifier.align(Alignment.Center).size(36.dp), color = Color.White, strokeWidth = 3.dp,
            )
        }
    }

    if (fullscreen) {
        Box(Modifier.fillMaxSize().background(Color.Black)) {
            video(Modifier.fillMaxSize())
            Row(
                Modifier.fillMaxWidth().background(Color.Black.copy(alpha = 0.55f)).displayCutoutPadding().padding(horizontal = 8.dp, vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(onClick = { fullscreen = false }) { Icon(Icons.Outlined.FullscreenExit, "退出全屏", tint = Color.White) }
                Text(task.title, modifier = Modifier.weight(1f).padding(horizontal = 8.dp), maxLines = 1, overflow = TextOverflow.Ellipsis, color = Color.White, style = MaterialTheme.typography.titleMedium)
                SubtitleMenu(choices, subtitleMode, selectSubtitle, light = true)
                SpeedMenu(speed, { speed = it }, light = true)
            }
            if (error != null) Text(
                error.orEmpty(),
                color = MaterialTheme.colorScheme.onErrorContainer,
                modifier = Modifier.align(Alignment.Center).background(ReadingColors.ErrorWash).padding(16.dp),
            )
        }
    } else {
        Scaffold(
            containerColor = ReadingColors.Paper,
            topBar = {
                TopAppBar(
                    title = { Text(if (nas == null) "正在播放" else "在线播放", style = MaterialTheme.typography.titleMedium) },
                    navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Outlined.ArrowBack, contentDescription = "返回片库") } },
                    colors = TopAppBarDefaults.topAppBarColors(containerColor = ReadingColors.Paper),
                )
            },
        ) { padding ->
            Box(Modifier.padding(padding).fillMaxSize()) {
                MaterialPlayerBody(video = video) {
                    Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
                        Row(verticalAlignment = Alignment.Top, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(task.title, style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                            IconButton(onClick = { fullscreen = true }) { Icon(Icons.Outlined.Fullscreen, contentDescription = "全屏播放") }
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(if (nas == null) Icons.Outlined.OfflinePin else Icons.Outlined.Dns, contentDescription = null, tint = ReadingColors.LocalTeal, modifier = Modifier.size(18.dp))
                            Text(if (nas == null) "已保存在手机，没网也能看" else "正在从家里的飞牛播放", style = MaterialTheme.typography.bodySmall, color = ReadingColors.LocalTeal)
                        }
                        if (error != null) Surface(color = ReadingColors.ErrorWash, shape = MaterialTheme.shapes.medium) {
                            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                Text(error.orEmpty(), color = ReadingColors.Error, style = MaterialTheme.typography.bodyMedium)
                                TextButton(onClick = { error = null; engine.prepare(); engine.play() }) { Text("重新播放") }
                            }
                        }
                        playbackActions?.invoke()
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text("字幕", style = MaterialTheme.typography.titleMedium)
                            Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                choices.forEach { choice ->
                                    MaterialFilterChip(
                                        selected = subtitleMode == choice.id, onClick = { selectSubtitle(choice.id) },
                                        label = { Text(choice.label) }, enabled = choice.available,
                                        modifier = Modifier.heightIn(min = 48.dp),
                                    )
                                }
                            }
                            if (nas == null && !task.bilingualPath.existsLocally()) Text(
                                if (task.subtitleState == TaskState.SKIPPED) "这个视频暂时没有字幕。"
                                else if (task.nasMediaId != null && task.subtitleState == TaskState.READY) "已保存所选字幕。需要中英双语时，可以在家庭影库中生成后再下载。"
                                else "中英字幕准备好后就能切换，你可以先开始看。",
                                style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                            subtitleActions?.invoke()
                        }
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Text("播放速度", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                            SpeedMenu(speed, { speed = it })
                        }
                        Text("离开播放页时会自动暂停，下次从上次的位置继续。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                    }
                }
            }
        }
    }
}

@Composable
private fun SpeedMenu(speed: Float, select: (Float) -> Unit, light: Boolean = false) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        TextButton(onClick = { expanded = true }, modifier = Modifier.heightIn(min = 48.dp)) {
            Text("${speed}×", color = if (light) Color.White else ReadingColors.CaptionBlue)
            Icon(Icons.Outlined.ExpandMore, contentDescription = "选择播放速度", tint = if (light) Color.White else ReadingColors.CaptionBlue, modifier = Modifier.size(18.dp))
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            listOf(0.5f, 0.75f, 1f, 1.25f, 1.5f, 1.75f, 2f).forEach { value ->
                DropdownMenuItem(text = { Text("${value}×" + if (value == speed) "  ✓" else "") }, onClick = { select(value); expanded = false })
            }
        }
    }
}

@Composable
private fun SubtitleMenu(choices: List<SubtitleChoice>, selected: String, select: (String) -> Unit, light: Boolean = false) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        TextButton(onClick = { expanded = true }, modifier = Modifier.heightIn(min = 48.dp)) {
            Text(choices.first { it.id == selected }.label, color = if (light) Color.White else ReadingColors.CaptionBlue)
            Icon(Icons.Outlined.ExpandMore, contentDescription = "选择字幕", tint = if (light) Color.White else ReadingColors.CaptionBlue, modifier = Modifier.size(18.dp))
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            choices.forEach { choice ->
                DropdownMenuItem(text = { Text(choice.label + if (choice.id == selected) "  ✓" else "") }, onClick = { select(choice.id); expanded = false }, enabled = choice.available)
            }
        }
    }
}

private fun String?.existsLocally(): Boolean = this?.let { File(it).isFile } == true
private tailrec fun Context.activity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.activity()
    else -> null
}

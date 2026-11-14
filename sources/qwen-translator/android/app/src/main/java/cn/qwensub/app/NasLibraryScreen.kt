package cn.qwensub.app

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items as gridItems
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
internal fun NasLibraryScreen(
    settings: ConnectionSettings,
    onSettings: () -> Unit,
    onPlay: (NasPlayback) -> Unit,
    onDownload: (NasPlayback) -> Unit,
    notify: (String) -> Unit,
) {
    val client = remember(settings) { MediaClient(settings) }
    var titleId by rememberSaveable { mutableStateOf<String?>(null) }
    var fileId by rememberSaveable { mutableStateOf<String?>(null) }
    var discoverKind by rememberSaveable { mutableStateOf<String?>(null) }
    var capabilities by remember(client) { mutableStateOf<Set<String>?>(null) }
    val jobs = remember { mutableStateMapOf<String, NasMediaJob>() }
    val requests = remember { mutableStateMapOf<String, String>() }
    when {
        discoverKind != null -> NasDiscoverScreen(client, discoverKind!!, onBack = { discoverKind = null },
            onOpen = { title -> titleId = title.id; discoverKind = null },
            onExisting = { id -> titleId = id; discoverKind = null }, notify = notify)
        fileId != null -> NasFileDetails(
            client, settings, fileId!!, jobs[fileId], requests[fileId],
            onBack = { fileId = null },
            onJob = { job, requestId -> jobs[fileId!!] = job; if (requestId != null) requests[fileId!!] = requestId },
            onPlay = onPlay, onDownload = onDownload,
        )
        titleId != null -> NasTitleDetails(client, titleId!!, capabilities, onBack = { titleId = null }, openFile = { fileId = it }, notify = notify)
        else -> NasCatalog(client, settings, onSettings, discover = { discoverKind = it },
            openTitle = { title -> titleId = title.id }, onCapabilities = { capabilities = it }, notify = notify,
        )
    }
}

@Composable
private fun NasCatalog(
    client: MediaClient, settings: ConnectionSettings, onSettings: () -> Unit,
    discover: (String) -> Unit, openTitle: (NasMediaTitle) -> Unit,
    onCapabilities: (Set<String>) -> Unit, notify: (String) -> Unit,
) {
    var catalog by remember(client) { mutableStateOf<NasMediaCatalog?>(null) }
    var queue by remember(client) { mutableStateOf<List<NasMediaQueueItem>>(emptyList()) }
    var selected by rememberSaveable { mutableStateOf("series") }
    var query by rememberSaveable { mutableStateOf("") }
    var refresh by remember { mutableIntStateOf(0) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var queueError by remember { mutableStateOf<String?>(null) }
    var queueRemoval by remember(client) { mutableStateOf<NasMediaQueueItem?>(null) }
    var removingQueue by remember(client) { mutableStateOf(false) }
    var removedQueueIds by remember(client) { mutableStateOf(emptySet<String>()) }
    val configured = settings.baseUrl.isNotBlank() && settings.token.isNotBlank()
    LaunchedEffect(client, refresh) {
        if (!configured) return@LaunchedEffect
        loading = true; error = null
        try { catalog = client.catalog().also { onCapabilities(it.capabilities) } }
        catch (cancel: CancellationException) { throw cancel }
        catch (problem: Exception) { error = safeError(problem) }
        finally { loading = false }
    }
    LaunchedEffect(client, selected, refresh) {
        if (!configured || selected != "queue") return@LaunchedEffect
        do {
            try {
                val incoming = client.queue()
                queue = incoming.filterNot { it.id in removedQueueIds }
                // Keep a confirmed deletion hidden until one fresh response no longer contains it.
                removedQueueIds = removedQueueIds.intersect(incoming.map { it.id }.toSet())
                queueError = null
            }
            catch (cancel: CancellationException) { throw cancel }
            catch (problem: Exception) { queueError = safeError(problem) }
            delay(8_000)
        } while (true)
    }
    val visible = catalog?.items.orEmpty().filter {
        it.kind == selected && (query.isBlank() || it.title.contains(query.trim(), ignoreCase = true) || it.year?.toString() == query.trim())
    }
    LazyVerticalGrid(columns = GridCells.Adaptive(300.dp), modifier = Modifier.fillMaxSize(), contentPadding = PaddingValues(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("家庭影库", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
                    Text("浏览家里飞牛上的剧集和电影", style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
                }
                IconButton(onClick = { refresh++ }, enabled = configured && !loading) { Icon(Icons.Outlined.Refresh, "刷新片库") }
            }
        }
        if (!configured) item(span = { GridItemSpan(maxLineSpan) }) {
            NasMessageCard("连接家里的飞牛", "连接后，就能在手机上浏览剧集和电影，也可以下载到手机离线观看。", onSettings, "去连接")
        }
        if (loading) item(span = { GridItemSpan(maxLineSpan) }) { LinearProgressIndicator(Modifier.fillMaxWidth()) }
        if (error != null) item(span = { GridItemSpan(maxLineSpan) }) { NasErrorCard(error!!, { refresh++ }) }
        catalog?.let { current ->
            if (!current.configured) item(span = { GridItemSpan(maxLineSpan) }) {
                NasMessageCard("影库还没有准备好", "请先在飞牛上完成影库设置，然后回到这里刷新。", { refresh++ }, "刷新")
            }
            if (current.services.isNotEmpty()) item(span = { GridItemSpan(maxLineSpan) }) {
                Surface(color = ReadingColors.Inset, shape = MaterialTheme.shapes.medium) {
                    Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        current.services.forEach { service ->
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.Top) {
                                Icon(if (service.connected) Icons.Outlined.CheckCircle else Icons.Outlined.Info,
                                    contentDescription = null, modifier = Modifier.size(18.dp), tint = if (service.connected) ReadingColors.LocalTeal else ReadingColors.Waiting)
                                Text("${if (service.kind == "sonarr") "剧集" else "电影"} · ${if (service.connected) "已连接" else "暂时无法连接"}",
                                    style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                            }
                        }
                    }
                }
            }
        }
        if (configured) {
            item(span = { GridItemSpan(maxLineSpan) }) {
                Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("series" to "剧集", "movie" to "电影", "queue" to "正在下载").forEach { (key, label) ->
                        MaterialFilterChip(selected = selected == key, onClick = { selected = key }, label = { Text(label) }, modifier = Modifier.heightIn(min = 48.dp))
                    }
                }
            }
            if (selected == "queue") {
                if (catalog != null && "manage_queue" !in catalog!!.capabilities) item(span = { GridItemSpan(maxLineSpan) }) {
                    NasMessageCard("更新飞牛服务以管理下载任务", "当前可以查看下载进度。更新飞牛上的服务后，还能在手机上删除下载。")
                }
                if (queueError != null) item(span = { GridItemSpan(maxLineSpan) }) { NasErrorCard(queueError!!, { refresh++ }) }
                if (queue.isEmpty() && queueError == null) item(span = { GridItemSpan(maxLineSpan) }) {
                    NasMessageCard("当前没有下载任务", "在剧集或电影详情中点“查找资源”，下载完成后就会出现在影库里。")
                }
                gridItems(queue.filterNot { it.id in removedQueueIds }, key = { "${it.kind}:${it.id}" }) { task ->
                    MaterialPanel() {
                        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Text(task.title, style = MaterialTheme.typography.titleMedium)
                            Text(if (task.kind == "sonarr") "剧集" else "电影", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                            LinearProgressIndicator(progress = { task.progress }, modifier = Modifier.fillMaxWidth())
                            Text("${(task.progress * 100).toInt()}%" + if (task.timeLeft.isBlank() || task.timeLeft == "null") "" else " · 剩余 ${task.timeLeft}", style = MaterialTheme.typography.bodySmall)
                            if (catalog?.capabilities?.contains("manage_queue") == true) {
                                OutlinedButton(onClick = { queueRemoval = task }, enabled = !removingQueue,
                                    modifier = Modifier.heightIn(min = 48.dp)) {
                                    Icon(Icons.Outlined.DeleteOutline, contentDescription = null, modifier = Modifier.size(20.dp))
                                    Spacer(Modifier.width(8.dp)); Text("删除任务")
                                }
                            }
                        }
                    }
                }
            } else {
                item(span = { GridItemSpan(maxLineSpan) }) {
                    OutlinedButton(onClick = { discover(if (selected == "series") "sonarr" else "radarr") }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                        Icon(Icons.Outlined.Add, contentDescription = null, modifier = Modifier.size(20.dp)); Spacer(Modifier.width(8.dp)); Text(if (selected == "series") "添加剧集" else "添加电影")
                    }
                }
                item(span = { GridItemSpan(maxLineSpan) }) {
                    OutlinedTextField(query, { query = it }, modifier = Modifier.fillMaxWidth(), singleLine = true,
                        label = { Text(if (selected == "series") "搜索剧集" else "搜索电影") },
                        leadingIcon = { Icon(Icons.Outlined.Search, contentDescription = null) })
                }
                if (catalog != null && visible.isEmpty() && !loading) item(span = { GridItemSpan(maxLineSpan) }) {
                    NasMessageCard(if (query.isNotBlank()) "没有匹配的内容" else "片库还没有${if (selected == "series") "剧集" else "电影"}",
                        if (query.isNotBlank()) "试试片名或年份。" else "点上方按钮添加想看的作品，下载完成后就能在这里观看。")
                }
                gridItems(visible, key = { it.id }) { title ->
                    Surface(onClick = { openTitle(title) }, color = ReadingColors.Sheet, shape = MaterialTheme.shapes.large, shadowElevation = 1.dp) {
                        Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                            Icon(if (title.kind == "series") Icons.Outlined.Tv else Icons.Outlined.Movie, contentDescription = null, tint = ReadingColors.CaptionBlue, modifier = Modifier.size(32.dp))
                            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                Text(title.title, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                                Text(listOfNotNull(title.year?.toString(), if (title.kind == "series") "${title.availableCount} / ${title.totalCount} 集可看" else if (title.mediaId != null) "可以观看" else "等待下载").joinToString(" · "),
                                    color = if (title.availableCount > 0 || title.mediaId != null) ReadingColors.LocalTeal else ReadingColors.SecondaryInk, style = MaterialTheme.typography.bodySmall)
                            }
                            Icon(Icons.Outlined.ChevronRight, contentDescription = "查看详情")
                        }
                    }
                }
            }
        }
    }
    queueRemoval?.let { task ->
        NasQueueRemovalDialog(client, task, onDismiss = { queueRemoval = null }, onBusyChange = { removingQueue = it },
            onRefresh = { queueRemoval = null; refresh++ }, onRemoved = { id ->
            removedQueueIds = removedQueueIds + id
            queue = queue.filterNot { it.id == id }
            queueRemoval = null; queueError = null; refresh++
            notify("下载任务已删除。")
        })
    }
}

@Composable
private fun NasTitleDetails(
    client: MediaClient, titleId: String, knownCapabilities: Set<String>?,
    onBack: () -> Unit, openFile: (String) -> Unit, notify: (String) -> Unit,
) {
    var title by remember(titleId) { mutableStateOf<NasMediaTitle?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(true) }
    var searching by remember { mutableStateOf(false) }
    var searchConfirmation by remember { mutableStateOf(false) }
    var refresh by remember { mutableIntStateOf(0) }
    var capabilities by remember(client) { mutableStateOf(knownCapabilities) }
    var capabilitiesError by remember(client) { mutableStateOf<String?>(null) }
    var managementAction by remember(titleId) { mutableStateOf<NasTitleManagementAction?>(null) }
    var managing by remember(titleId) { mutableStateOf(false) }
    var season by rememberSaveable(titleId) { mutableIntStateOf(-1) }
    val scope = rememberCoroutineScope()
    BackHandler(onBack = { if (!managing) onBack() })
    LaunchedEffect(client, titleId, refresh) {
        loading = true; error = null
        try { title = client.title(titleId) }
        catch (cancel: CancellationException) { throw cancel }
        catch (problem: Exception) { error = safeError(problem) }
        finally { loading = false }
    }
    LaunchedEffect(client, refresh) {
        if (capabilities != null) return@LaunchedEffect
        try { capabilities = client.catalog().capabilities; capabilitiesError = null }
        catch (cancel: CancellationException) { throw cancel }
        catch (problem: Exception) { capabilitiesError = safeError(problem) }
    }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item { NasDetailHeading(title?.title ?: "作品详情", { if (!managing) onBack() }, { refresh++ }, loading || managing) }
        if (loading) item { LinearProgressIndicator(Modifier.fillMaxWidth()) }
        if (error != null) item { NasErrorCard(error!!, { refresh++ }) }
        title?.let { current ->
            item {
                Text(listOfNotNull(current.year?.toString(), if (current.monitored) "自动追更中" else "已暂停追更").joinToString(" · "), style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
            }
            if (current.overview.isNotBlank()) item { Text(current.overview, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk) }
            item {
                OutlinedButton(onClick = { searchConfirmation = true }, enabled = !loading && !searching && !managing, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Icon(Icons.Outlined.Search, contentDescription = null, modifier = Modifier.size(20.dp)); Spacer(Modifier.width(8.dp))
                    Text(if (searching) "正在查找…" else if (current.kind == "series") "查找缺少的剧集" else "查找电影资源")
                }
                Text("将使用你在飞牛上设置好的下载偏好。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk, modifier = Modifier.padding(top = 6.dp))
            }
            if (capabilities?.contains("manage_titles") == true) item {
                Surface(color = ReadingColors.Inset, shape = MaterialTheme.shapes.large) {
                    Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("管理作品", style = MaterialTheme.typography.titleSmall)
                        OutlinedButton(onClick = { managementAction = NasTitleManagementAction.MONITOR }, enabled = !loading && !searching && !managing,
                            modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                            Text(if (current.monitored) "停止自动追更" else "恢复自动追更")
                        }
                        TextButton(onClick = { managementAction = NasTitleManagementAction.REMOVE }, enabled = !loading && !searching && !managing,
                            modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                            Icon(Icons.Outlined.DeleteOutline, contentDescription = null, modifier = Modifier.size(20.dp))
                            Spacer(Modifier.width(8.dp)); Text("移除作品", color = ReadingColors.Error)
                        }
                    }
                }
            } else if (capabilities != null) item {
                NasMessageCard("更新飞牛服务以管理作品", "更新飞牛上的服务后，可以在手机上暂停追更或移除作品。")
            }
            if (capabilitiesError != null) item { NasErrorCard(capabilitiesError!!, { refresh++ }) }
            if (current.kind == "movie" && current.mediaId != null) item {
                Button(onClick = { openFile(current.mediaId) }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("观看电影") }
            }
            current.searchMessage?.takeIf { it.isNotBlank() }?.let { message ->
                item { Text(message, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk) }
            }
            val seasons = current.episodes.map { it.seasonNumber }.distinct().sorted()
            if (seasons.isNotEmpty()) item {
                Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MaterialFilterChip(selected = season == -1, onClick = { season = -1 }, label = { Text("全部") }, modifier = Modifier.heightIn(min = 48.dp))
                    seasons.forEach { value -> MaterialFilterChip(selected = season == value, onClick = { season = value }, label = { Text(if (value == 0) "特别篇" else "第 $value 季") }, modifier = Modifier.heightIn(min = 48.dp)) }
                }
            }
            items(current.episodes.filter { season == -1 || it.seasonNumber == season }.sortedWith(compareBy({ it.seasonNumber }, { it.episodeNumber })), key = { it.id }) { episode ->
                Surface(color = ReadingColors.Sheet, shape = MaterialTheme.shapes.medium) {
                    Row(Modifier.fillMaxWidth().then(if (episode.mediaId != null) Modifier.clickable { openFile(episode.mediaId) } else Modifier).padding(16.dp),
                        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                            Text(episode.episodeLabel, style = MaterialTheme.typography.labelLarge, color = ReadingColors.CaptionBlue)
                            Text(episode.title.ifBlank { "第 ${episode.episodeNumber} 集" }, style = MaterialTheme.typography.titleSmall)
                            Text(if (episode.hasFile) "可以观看" else if (episode.monitored) "等待下载" else "暂未下载", style = MaterialTheme.typography.bodySmall,
                                color = if (episode.hasFile) ReadingColors.LocalTeal else ReadingColors.SecondaryInk)
                        }
                        if (episode.mediaId != null) Icon(Icons.Outlined.PlayCircle, contentDescription = "查看剧集", tint = ReadingColors.CaptionBlue)
                    }
                }
            }
            if (current.kind == "series" && current.episodes.isEmpty() && !loading) item { NasMessageCard("还没有剧集信息", "请稍后刷新再看看。") }
        }
    }
    val currentTitle = title
    val action = managementAction
    if (currentTitle != null && action != null) NasTitleManagementDialog(
        client, currentTitle, action,
        onDismiss = { managementAction = null }, onBusyChange = { managing = it },
        onRefresh = { managementAction = null; title = null; refresh++ },
        onUpdated = { updated ->
            title = updated; managementAction = null; refresh++
            notify(if (updated.monitored) "已恢复自动追更。" else "已停止自动追更。")
        },
        onRemoved = {
            title = null; managementAction = null
            notify("作品已从管理列表移除。")
            onBack()
        },
    )
    if (searchConfirmation) AlertDialog(
        onDismissRequest = { searchConfirmation = false }, title = { Text("查找“${title?.title.orEmpty()}”？") },
        text = { Text(if (title?.kind == "series") "飞牛会查找这部剧还缺少的集数，并在找到后自动下载。" else "飞牛会查找这部电影，并在找到后自动下载。") },
        confirmButton = { TextButton(onClick = {
            searchConfirmation = false
            scope.launch {
                searching = true
                try { notify(client.search(titleId)); refresh++ }
                catch (cancel: CancellationException) { throw cancel }
                catch (problem: Exception) { error = safeError(problem) }
                finally { searching = false }
            }
        }) { Text("开始查找") } },
        dismissButton = { TextButton(onClick = { searchConfirmation = false }) { Text("取消") } },
    )
}

@Composable
private fun NasFileDetails(
    client: MediaClient, settings: ConnectionSettings, fileId: String, cachedJob: NasMediaJob?, cachedRequest: String?,
    onBack: () -> Unit, onJob: (NasMediaJob, String?) -> Unit,
    onPlay: (NasPlayback) -> Unit, onDownload: (NasPlayback) -> Unit,
) {
    var file by remember(fileId) { mutableStateOf<NasMediaFile?>(null) }
    var job by remember(fileId) { mutableStateOf(cachedJob) }
    var requestId by rememberSaveable(fileId) { mutableStateOf(cachedRequest) }
    var selected by rememberSaveable(fileId) { mutableStateOf("auto") }
    var loading by remember { mutableStateOf(true) }
    var preparing by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var jobError by remember { mutableStateOf<String?>(null) }
    var refresh by remember { mutableIntStateOf(0) }
    val context = LocalContext.current
    val requestStore = remember(context) { MediaRequestStore(context) }
    val scope = rememberCoroutineScope()
    BackHandler(onBack = onBack)
    LaunchedEffect(client, fileId, refresh) {
        loading = true; error = null
        try {
            val loaded = client.file(fileId)
            if (loaded.subtitles.isEmpty()) {
                file = loaded.copy(job = null)
                job = null
                requestId = null
            } else {
                file = loaded
                loaded.job?.let { if (job == null) { job = it; requestId = it.clientRequestId ?: requestId ?: requestStore.saved(settings, fileId); onJob(it, requestId) } }
            }
        }
        catch (cancel: CancellationException) { throw cancel }
        catch (problem: Exception) { error = safeError(problem) }
        finally { loading = false }
    }
    LaunchedEffect(client, job?.id, refresh) {
        val id = job?.id ?: return@LaunchedEffect
        while (true) {
            try {
                val updated = client.job(id)
                job = updated; onJob(updated, requestId); jobError = null
                if (updated.finished) { if (updated.ready) { selected = "generated-${id}-bilingual" }; break }
            } catch (cancel: CancellationException) { throw cancel }
            catch (problem: Exception) { jobError = safeError(problem) }
            delay(3_000)
        }
    }
    val subtitles = ((if (job?.ready == true) generatedMediaSubtitles(job!!.id) else emptyList()) + file?.subtitles.orEmpty()).distinctBy { it.downloadPath }
    val selectedId = if (selected == "auto") subtitles.firstOrNull()?.id else selected.takeUnless { it == "off" }
    val active = job != null && !job!!.finished
    val playback = file?.let { NasPlayback(it.copy(job = job), subtitles, selectedId, settings, job?.id, job?.clientRequestId ?: requestId) }
    fun prepare() {
        val media = file ?: return
        val track = subtitles.firstOrNull { it.id == selectedId && it.source != "generated" }
            ?: subtitles.firstOrNull { it.source != "generated" }
            ?: return
        val id = try {
            requestStore.request(settings, media.id, track.id, false, null, replace = job?.finished == true)
        } catch (problem: Exception) {
            jobError = safeError(problem)
            return
        }
        requestId = id
        scope.launch {
            preparing = true
            jobError = null
            try {
                val result = client.prepare(media.id, track.id, requestId = id)
                job = result
                onJob(result, id)
            } catch (cancel: CancellationException) { throw cancel }
            catch (problem: Exception) { jobError = safeError(problem) }
            finally { preparing = false }
        }
    }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item { NasDetailHeading(file?.title ?: "视频详情", onBack, { refresh++ }, loading) }
        if (loading) item { LinearProgressIndicator(Modifier.fillMaxWidth()) }
        if (error != null) item { NasErrorCard(error!!, { refresh++ }) }
        file?.let { media ->
            item {
                MaterialPanel() {
                    Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        Text(media.title, style = MaterialTheme.typography.titleLarge)
                        Text(listOfNotNull(mediaSize(media.size), if (media.duration > 0) "${(media.duration / 60).toInt()} 分钟" else null).joinToString(" · "), style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(onClick = { playback?.let(onPlay) }, modifier = Modifier.weight(1f).heightIn(min = 48.dp)) {
                                Icon(Icons.Outlined.PlayArrow, contentDescription = null, modifier = Modifier.size(20.dp)); Spacer(Modifier.width(4.dp)); Text("立即观看")
                            }
                            OutlinedButton(onClick = { playback?.let(onDownload) }, modifier = Modifier.weight(1f).heightIn(min = 48.dp)) {
                                Icon(Icons.Outlined.Download, contentDescription = null, modifier = Modifier.size(18.dp)); Spacer(Modifier.width(4.dp)); Text("存到手机")
                            }
                        }
                        Text("在线观看需要连接飞牛；存到手机后，没网也能看。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                    }
                }
            }
            if (media.compatibleAvailable) item {
                NasCompatibilityControls(media, settings,
                    onReady = { compatible -> playback?.copy(file = compatible)?.let(onPlay) },
                    actionLabel = "没有声音？试试修复播放",
                    onDownload = { compatible -> playback?.copy(file = compatible)?.let(onDownload) },
                )
            }
            item {
                Text("选择字幕", style = MaterialTheme.typography.titleMedium, modifier = Modifier.semantics { heading() })
                Text("选择你想看的字幕。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk, modifier = Modifier.padding(top = 6.dp))
            }
            items(subtitles, key = { it.id }) { track ->
                Surface(onClick = { selected = track.id }, color = if (selectedId == track.id) ReadingColors.BlueWash else ReadingColors.Sheet, shape = MaterialTheme.shapes.medium) {
                    Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp).heightIn(min = 56.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        RadioButton(selected = selectedId == track.id, onClick = { selected = track.id })
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                            Text(track.title.ifBlank { track.language.ifBlank { "未命名字幕" } }, style = MaterialTheme.typography.bodyMedium)
                            Text(listOf(track.language, when (track.source) { "embedded" -> "视频自带"; "generated" -> "翻译字幕"; else -> "字幕文件" }).filter { it.isNotBlank() }.joinToString(" · "), style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                        }
                    }
                }
            }
            item {
                Surface(onClick = { selected = "off" }, color = if (selectedId == null) ReadingColors.BlueWash else ReadingColors.Sheet, shape = MaterialTheme.shapes.medium) {
                    Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp).heightIn(min = 56.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        RadioButton(selected = selectedId == null, onClick = { selected = "off" }); Text("关闭字幕")
                    }
                }
            }
            if (subtitles.isEmpty()) item {
                NasMessageCard("没有字幕", "这部视频暂时没有字幕，但仍可观看或存到手机。")
            }
            if (media.subtitles.any { it.source != "generated" }) item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { prepare() }, enabled = !preparing && !active, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                        if (preparing) { CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary); Spacer(Modifier.width(8.dp)) }
                        else { Icon(Icons.Outlined.Translate, contentDescription = null, modifier = Modifier.size(20.dp)); Spacer(Modifier.width(8.dp)) }
                        Text(if (preparing) "正在开始…" else "生成中文字幕")
                    }
                    Text("会根据所选字幕生成中文和中英双语字幕。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                }
            }
        }
        if (jobError != null) item { NasErrorCard(jobError!!, { refresh++ }) }
        job?.takeIf { file?.subtitles?.isNotEmpty() == true }?.let { current -> item {
            Surface(color = if (current.state == "failed") ReadingColors.ErrorWash else ReadingColors.TealWash, shape = MaterialTheme.shapes.large) {
                Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text(when (current.state) { "ready" -> "中文字幕已准备好"; "failed" -> "字幕生成未完成"; "cancelled" -> "已停止生成字幕"; "queued" -> "即将开始生成字幕"; else -> "正在生成中文字幕" }, style = MaterialTheme.typography.titleSmall)
                    if (!current.finished) LinearProgressIndicator(progress = { current.progress }, modifier = Modifier.fillMaxWidth())
                    if (current.message.isNotBlank()) Text(current.message, style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                    if (current.state in setOf("failed", "cancelled")) OutlinedButton(onClick = {
                        scope.launch {
                            preparing = true
                            try {
                                val next = client.retry(current.id)
                                job = next
                                onJob(next, requestId)
                                jobError = null
                                refresh++
                            } catch (cancel: CancellationException) { throw cancel }
                            catch (problem: Exception) { jobError = safeError(problem) }
                            finally { preparing = false }
                        }
                    }, enabled = !preparing, modifier = Modifier.heightIn(min = 48.dp)) { Text("重试字幕") }
                    if (!current.finished) TextButton(onClick = {
                        scope.launch {
                            try { client.cancel(current.id); refresh++ }
                            catch (cancel: CancellationException) { throw cancel }
                            catch (problem: Exception) { jobError = safeError(problem) }
                        }
                    }, modifier = Modifier.heightIn(min = 48.dp)) { Text("停止生成") }
                }
            }
        } }
    }
}

@Composable
private fun NasDetailHeading(title: String, onBack: () -> Unit, refresh: () -> Unit, loading: Boolean) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Outlined.ArrowBack, "返回家庭影库") }
        Text(title, style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f), maxLines = 2, overflow = TextOverflow.Ellipsis)
        IconButton(onClick = refresh, enabled = !loading) { Icon(Icons.Outlined.Refresh, "刷新详情") }
    }
}

@Composable
private fun NasMessageCard(title: String, body: String, action: (() -> Unit)? = null, actionLabel: String = "") {
    Surface(color = ReadingColors.Inset, shape = MaterialTheme.shapes.large) {
        Column(Modifier.fillMaxWidth().padding(20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(body, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
            if (action != null) OutlinedButton(onClick = action, modifier = Modifier.heightIn(min = 48.dp)) { Text(actionLabel) }
        }
    }
}

@Composable
private fun NasErrorCard(message: String, retry: () -> Unit) {
    Surface(color = ReadingColors.ErrorWash, shape = MaterialTheme.shapes.medium) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(message, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.Error)
            TextButton(onClick = retry, modifier = Modifier.heightIn(min = 48.dp)) { Icon(Icons.Outlined.Refresh, contentDescription = null, modifier = Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)); Text("重新加载") }
        }
    }
}

private fun mediaSize(size: Long): String = if (size >= 1_073_741_824L) String.format(java.util.Locale.CHINA, "%.1f GB", size / 1_073_741_824.0)
    else String.format(java.util.Locale.CHINA, "%.0f MB", size / 1_048_576.0)

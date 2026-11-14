package cn.qwensub.app

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

@Composable
internal fun NasDiscoverScreen(
    client: MediaClient, kind: String, onBack: () -> Unit,
    onOpen: (NasMediaTitle) -> Unit, onExisting: (String) -> Unit, notify: (String) -> Unit,
) {
    val context = LocalContext.current
    val savedSelection = remember(client.selectionScope, kind) { MediaSelectionSettings(context, client.selectionScope, kind) }
    var query by rememberSaveable(kind) { mutableStateOf("") }
    var results by remember(kind) { mutableStateOf<List<NasDiscoveredTitle>>(emptyList()) }
    var options by remember(kind) { mutableStateOf<NasMediaAddOptions?>(null) }
    var qualityId by rememberSaveable(client.selectionScope, kind) { mutableStateOf<Int?>(null) }
    var rootId by rememberSaveable(client.selectionScope, kind) { mutableStateOf<Int?>(null) }
    var selectedId by rememberSaveable(kind) { mutableStateOf<Int?>(null) }
    var searched by remember { mutableStateOf(false) }
    var searching by remember { mutableStateOf(false) }
    var adding by remember { mutableStateOf(false) }
    var loadingOptions by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var optionsError by remember { mutableStateOf<String?>(null) }
    var refreshOptions by remember { mutableIntStateOf(0) }
    val scope = rememberCoroutineScope()
    BackHandler(onBack = onBack)
    LaunchedEffect(client, kind, refreshOptions) {
        loadingOptions = true
        try {
            val loaded = client.options(kind); options = loaded; optionsError = null
            qualityId = resolveMediaSelection(loaded.qualityProfiles, qualityId ?: savedSelection.quality(), loaded.defaultQualityProfileId)
            rootId = resolveMediaSelection(loaded.rootFolders, rootId ?: savedSelection.root())
        } catch (cancel: CancellationException) { throw cancel }
        catch (problem: Exception) { optionsError = safeError(problem) }
        finally { loadingOptions = false }
    }
    fun search() {
        scope.launch {
            searching = true; error = null; selectedId = null
            try { results = client.discover(kind, query); searched = true }
            catch (cancel: CancellationException) { throw cancel }
            catch (problem: Exception) { error = safeError(problem) }
            finally { searching = false }
        }
    }
    LazyColumn(Modifier.fillMaxSize().imePadding(), contentPadding = PaddingValues(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Outlined.ArrowBack, "返回家庭影库") }
                Text(if (kind == "sonarr") "添加剧集" else "添加电影", style = MaterialTheme.typography.headlineSmall)
            }
            Text("搜索想看的作品，添加后飞牛会自动查找并下载。", style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
        }
        item {
            OutlinedTextField(query, { query = it }, label = { Text("作品名称") }, singleLine = true, enabled = !adding && !searching,
                modifier = Modifier.fillMaxWidth(), leadingIcon = { Icon(Icons.Outlined.Search, contentDescription = null) })
            Button(onClick = ::search, enabled = query.isNotBlank() && !searching && !adding, modifier = Modifier.fillMaxWidth().padding(top = 8.dp).heightIn(min = 48.dp)) {
                if (searching) { CircularProgressIndicator(Modifier.size(18.dp), color = MaterialTheme.colorScheme.onPrimary, strokeWidth = 2.dp); Spacer(Modifier.width(8.dp)) }
                Text(if (searching) "正在搜索…" else "搜索作品")
            }
        }
        if (error != null) item {
            Surface(color = ReadingColors.ErrorWash, shape = MaterialTheme.shapes.medium) {
                Text(error!!, modifier = Modifier.fillMaxWidth().padding(16.dp), color = ReadingColors.Error, style = MaterialTheme.typography.bodyMedium)
            }
        }
        if (searched && results.isEmpty() && !searching) item { Text("没有找到相关作品，试试英文或原始片名。", style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk) }
        items(results, key = { "${it.kind}:${it.externalId}" }) { item ->
            MaterialPanel() {
                Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text(item.title + (item.year?.let { "（$it）" } ?: ""), style = MaterialTheme.typography.titleMedium)
                    if (item.overview.isNotBlank()) Text(item.overview, style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                    if (item.alreadyAdded && item.titleId != null) {
                        OutlinedButton(onClick = { onExisting(item.titleId) }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("已在影库中 · 查看") }
                    } else if (selectedId != item.externalId) {
                        OutlinedButton(onClick = { selectedId = item.externalId }, enabled = !adding, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("添加这个作品") }
                    } else {
                        if (loadingOptions) LinearProgressIndicator(Modifier.fillMaxWidth())
                        if (optionsError != null) {
                            Text(optionsError!!, style = MaterialTheme.typography.bodySmall, color = ReadingColors.Error)
                            TextButton(onClick = { refreshOptions++ }) { Text("重新读取") }
                        }
                        options?.let { available ->
                            if (available.qualityProfiles.isEmpty() || available.rootFolders.isEmpty()) {
                                Text("飞牛上的下载设置还没有完成，请先在飞牛上完成设置。", style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
                            } else {
                                NasOptionSelector("画质偏好", available.qualityProfiles, qualityId, enabled = !adding, choose = { qualityId = it; savedSelection.saveQuality(it) })
                                available.qualityProfiles.firstOrNull { it.id == qualityId }?.description?.takeIf { it.isNotBlank() }?.let {
                                    Text(it, style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                                }
                                NasOptionSelector("保存位置", available.rootFolders, rootId, enabled = !adding, choose = { rootId = it; savedSelection.saveRoot(it) })
                                Text("下次添加时会自动使用这次的选择。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                                Text("添加后会自动追更，并立即开始查找资源。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                                Button(onClick = {
                                    val quality = qualityId; val root = rootId
                                    if (quality != null && root != null) scope.launch {
                                        adding = true; error = null
                                        try {
                                            val added = client.add(kind, item.externalId, quality, root)
                                            savedSelection.saveQuality(quality); savedSelection.saveRoot(root)
                                            notify(added.searchMessage ?: "已添加，正在查找资源。")
                                            onOpen(added)
                                        } catch (cancel: CancellationException) { throw cancel }
                                        catch (problem: Exception) { error = safeError(problem) }
                                        finally { adding = false }
                                    }
                                }, enabled = !adding && qualityId != null && rootId != null, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                                    if (adding) { CircularProgressIndicator(Modifier.size(18.dp), color = MaterialTheme.colorScheme.onPrimary, strokeWidth = 2.dp); Spacer(Modifier.width(8.dp)) }
                                    Text(if (adding) "正在添加…" else "添加并开始下载")
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun NasOptionSelector(label: String, options: List<NasMediaOption>, selected: Int?, enabled: Boolean, choose: (Int) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(label, style = MaterialTheme.typography.labelLarge)
        Box {
            OutlinedButton(onClick = { expanded = true }, enabled = enabled, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                Text(options.firstOrNull { it.id == selected }?.name ?: "请选择$label", modifier = Modifier.weight(1f))
                Icon(Icons.Outlined.ExpandMore, "选择$label")
            }
            DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                options.forEach { option -> DropdownMenuItem(text = { Text(option.name) }, onClick = { choose(option.id); expanded = false }) }
            }
        }
    }
}

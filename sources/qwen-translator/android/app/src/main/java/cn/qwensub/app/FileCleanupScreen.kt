package cn.qwensub.app

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.DeleteOutline
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

internal fun cleanupStatus(task: DownloadTask): String = listOf(
    when {
        task.localCleanupPending -> "手机：等待删除"
        task.localFilesDeleted -> "手机：已删除"
        else -> "手机：已保存"
    },
    when {
        task.remoteCleanupPending -> "飞牛：等待删除字幕"
        task.remoteFilesDeleted -> "飞牛：字幕已删除"
        task.nasBaseUrl != null -> "飞牛：保存了字幕"
        else -> "飞牛：没有保存内容"
    },
).joinToString("\n")

@Composable
internal fun TaskCleanupDialog(task: DownloadTask, repository: AppRepository, enabled: Boolean,
    onDismiss: () -> Unit, notify: (String) -> Unit) {
    var local by rememberSaveable(task.id) { mutableStateOf(!task.localFilesDeleted) }
    var remote by rememberSaveable(task.id) { mutableStateOf(false) }
    var error by remember(task.id) { mutableStateOf<String?>(null) }
    val pending = task.localCleanupPending || task.remoteCleanupPending
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(Icons.Outlined.DeleteOutline, contentDescription = null) },
        title = { Text(if (pending) "删除进度" else "删除这个视频的文件") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(task.title, style = MaterialTheme.typography.titleMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
                Text(cleanupStatus(task), style = MaterialTheme.typography.bodySmall)
                if (!pending) {
                    if (!task.localFilesDeleted) CleanupChoice("手机上的文件", "视频和字幕", local) { local = it }
                    if (!task.remoteFilesDeleted) CleanupChoice("飞牛上的字幕", "原语言字幕和翻译结果", remote) { remote = it }
                    Text("删除后无法恢复。通过“另存为”保存到其他文件夹的文件不会受影响。")
                    if (remote) Text("如果飞牛暂时连不上，会在恢复连接后自动删除。")
                } else {
                    Text("已开始删除。飞牛上的字幕会在连接恢复后完成删除。")
                }
                (error ?: task.cleanupError)?.let { Text(it, color = ReadingColors.Error) }
            }
        },
        confirmButton = {
            if (pending) {
                TextButton(onClick = {
                    runCatching { repository.retryCleanup(task.id) }
                        .onSuccess { onDismiss(); notify("已重新开始删除。") }
                        .onFailure { error = safeError(it) }
                }, enabled = enabled && task.cleanupError != null) { Text("重新删除") }
            } else {
                TextButton(onClick = {
                    runCatching { repository.requestCleanup(listOf(task.id), local && !task.localFilesDeleted, remote && !task.remoteFilesDeleted) }
                        .onSuccess { onDismiss(); notify("已开始删除。") }
                        .onFailure { error = safeError(it) }
                }, enabled = enabled && (local && !task.localFilesDeleted || remote && !task.remoteFilesDeleted)) {
                    Text("确认删除", color = MaterialTheme.colorScheme.error)
                }
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(if (pending) "关闭" else "取消") } },
    )
}

@Composable
internal fun FileCleanupScreen(repository: AppRepository, enabled: Boolean, onBack: () -> Unit) {
    val tasks by repository.tasks.collectAsState()
    var selectedIds by rememberSaveable { mutableStateOf(emptyList<String>()) }
    var local by rememberSaveable { mutableStateOf(true) }
    var remote by rememberSaveable { mutableStateOf(false) }
    var confirmIds by rememberSaveable { mutableStateOf(emptyList<String>()) }
    var message by rememberSaveable { mutableStateOf<String?>(null) }
    val selectable = tasks.filter {
        !it.localCleanupPending && !it.remoteCleanupPending &&
            (local && !it.localFilesDeleted || remote && !it.remoteFilesDeleted)
    }.map { it.id }
    val selected = selectedIds.filter { it in selectable }
    BackHandler(onBack = onBack)

    LazyColumn(
        modifier = Modifier.fillMaxSize(), contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        item {
            TextButton(onClick = onBack, contentPadding = PaddingValues(0.dp)) {
                Icon(Icons.AutoMirrored.Outlined.ArrowBack, contentDescription = null)
                Spacer(Modifier.width(8.dp)); Text("返回设置")
            }
            Text("管理已下载内容", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
            Text("选择要删除的视频和位置，释放存储空间。", modifier = Modifier.padding(top = 8.dp),
                style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
        }
        item {
            MaterialPanel() {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("删除哪些内容", style = MaterialTheme.typography.titleMedium)
                    CleanupChoice("手机上的文件", "视频、字幕和临时文件", local) { local = it }
                    CleanupChoice("飞牛上的字幕", "这些视频在飞牛上保存的字幕", remote) { remote = it }
                    Text("通过“另存为”保存的文件和连接设置会保留。",
                        style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                    if (remote) Text("如果飞牛暂时连不上，会在恢复连接后自动删除。",
                        style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                }
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("已选 ${selected.size} 个视频", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = { selectedIds = if (selected.size == selectable.size) emptyList() else selectable },
                    enabled = selectable.isNotEmpty() && enabled) {
                    Text(if (selected.isNotEmpty() && selected.size == selectable.size) "取消全选" else "全选")
                }
            }
            Button(
                onClick = { confirmIds = selected; message = null },
                enabled = enabled && selected.isNotEmpty() && (local || remote),
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error),
            ) {
                Icon(Icons.Outlined.DeleteOutline, contentDescription = null, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp)); Text("删除所选内容…")
            }
            if (!enabled) Text("正在另存文件，完成后可以删除。", modifier = Modifier.padding(top = 8.dp), style = MaterialTheme.typography.bodySmall)
            if (message != null) Text(message.orEmpty(), modifier = Modifier.padding(top = 8.dp), style = MaterialTheme.typography.bodyMedium)
        }
        if (tasks.isEmpty()) item {
            Text("还没有已下载的视频。", color = ReadingColors.SecondaryInk)
        }
        items(tasks, key = { it.id }) { task ->
            val canSelect = enabled && task.id in selectable
            MaterialPanel() {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                            .toggleable(value = task.id in selected, enabled = canSelect, role = Role.Checkbox) { checked ->
                                selectedIds = if (checked) (selectedIds + task.id).distinct() else selectedIds - task.id
                            },
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        Checkbox(checked = task.id in selected, onCheckedChange = null, enabled = canSelect)
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text(task.title, style = MaterialTheme.typography.titleMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
                            Text(formatCleanupDate(task.createdAt), style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                        }
                    }
                    Text(cleanupStatus(task), style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                    if (!task.cleanupError.isNullOrBlank()) {
                        Text(task.cleanupError, style = MaterialTheme.typography.bodySmall, color = ReadingColors.Error)
                        if (task.localCleanupPending || task.remoteCleanupPending) {
                            TextButton(onClick = {
                                runCatching { repository.retryCleanup(task.id) }
                                    .onSuccess { message = "已重新开始删除。" }
                                    .onFailure { message = safeError(it) }
                            }, enabled = enabled) { Text("重新删除") }
                        }
                    }
                }
            }
        }
    }
    if (confirmIds.isNotEmpty()) {
        val locations = when {
            local && remote -> "手机上的视频和字幕，以及飞牛上的字幕"
            local -> "手机上的视频和字幕"
            else -> "飞牛上的字幕"
        }
        AlertDialog(
            onDismissRequest = { confirmIds = emptyList() },
            icon = { Icon(Icons.Outlined.DeleteOutline, contentDescription = null) },
            title = { Text("删除 ${confirmIds.size} 个视频的文件？") },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("将删除$locations，删除后无法恢复。")
                    Text(if (local) "这些视频的下载和字幕生成会先停止。" else "手机上已保存的视频和字幕会保留。")
                    Text("通过“另存为”保存的文件和连接设置会保留。")
                    if (remote) Text("如果飞牛暂时连不上，会在恢复连接后自动删除。")
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    runCatching { repository.requestCleanup(confirmIds, local, remote) }
                        .onSuccess {
                            selectedIds = emptyList()
                            confirmIds = emptyList()
                            message = "已开始删除。"
                        }.onFailure {
                            confirmIds = emptyList()
                            message = safeError(it)
                        }
                }, enabled = enabled) { Text("确认删除", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { confirmIds = emptyList() }) { Text("保留文件") } },
        )
    }
}

@Composable
private fun CleanupChoice(title: String, detail: String, checked: Boolean, change: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().heightIn(min = 64.dp).toggleable(value = checked, role = Role.Checkbox, onValueChange = change),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Checkbox(checked = checked, onCheckedChange = null)
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(detail, style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
        }
    }
}

private fun formatCleanupDate(time: Long): String =
    java.text.SimpleDateFormat("M 月 d 日 HH:mm 添加", java.util.Locale.CHINA).format(java.util.Date(time))

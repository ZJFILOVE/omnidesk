package cn.qwensub.app

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

internal enum class NasTitleManagementAction { MONITOR, REMOVE }

/** Kept outside the lazy queue rows so scrolling or polling cannot dismiss an active request. */
@Composable
internal fun NasQueueRemovalDialog(
    client: MediaClient,
    task: NasMediaQueueItem,
    onDismiss: () -> Unit,
    onBusyChange: (Boolean) -> Unit,
    onRefresh: () -> Unit,
    onRemoved: (String) -> Unit,
) {
    var blocklist by remember(task.id) { mutableStateOf(false) }
    var busy by remember(task.id) { mutableStateOf(false) }
    var error by remember(task.id) { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("删除下载任务？") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(task.title, style = MaterialTheme.typography.titleSmall)
                Text("将停止这个下载，并删除尚未完成的下载内容。如果是一整季合集，其中所有集数都会受影响。")
                Text("影库里已经能看的视频，以及手机上保存的视频都不会被删除。")
                NasManagementCheckbox("以后不再下载这个资源", blocklist, !busy) { blocklist = it }
                NasManagementError(error, "刷新下载列表", onRefresh)
            }
        },
        confirmButton = {
            TextButton(enabled = !busy, onClick = {
                if (!busy) {
                    busy = true; error = null; onBusyChange(true)
                    scope.launch {
                        try {
                            client.removeQueueItem(task.id, blocklist)
                            onRemoved(task.id)
                        } catch (cancel: CancellationException) { throw cancel }
                        catch (problem: Exception) { error = safeError(problem) }
                        finally { busy = false; onBusyChange(false) }
                    }
                }
            }) { Text(if (busy) "正在删除…" else "删除任务", color = ReadingColors.Error) }
        },
        dismissButton = { TextButton(enabled = !busy, onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
internal fun NasTitleManagementDialog(
    client: MediaClient,
    title: NasMediaTitle,
    action: NasTitleManagementAction,
    onDismiss: () -> Unit,
    onBusyChange: (Boolean) -> Unit,
    onRefresh: () -> Unit,
    onUpdated: (NasMediaTitle) -> Unit,
    onRemoved: () -> Unit,
) {
    var deleteFiles by remember(title.id, action) { mutableStateOf(false) }
    var addImportExclusion by remember(title.id, action) { mutableStateOf(true) }
    var busy by remember(title.id, action) { mutableStateOf(false) }
    var error by remember(title.id, action) { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val removing = action == NasTitleManagementAction.REMOVE
    val monitorLabel = if (title.monitored) "停止自动追更" else "恢复自动追更"
    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text(if (removing) "移除作品？" else "$monitorLabel？") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(title.title, style = MaterialTheme.typography.titleSmall)
                if (removing) {
                    Text("将从影库中移除这个作品。默认会保留飞牛上的视频文件；正在进行的下载需要另外删除。")
                    NasManagementCheckbox("同时删除飞牛上的视频文件", deleteFiles, !busy) { deleteFiles = it }
                    if (deleteFiles) Text("视频文件删除后无法恢复。", color = ReadingColors.Error)
                    NasManagementCheckbox("以后不要自动重新添加", addImportExclusion, !busy) { addImportExclusion = it }
                    Text("手机上保存的视频不会受影响。")
                } else {
                    Text(if (title.monitored) "之后不会再自动下载这个作品的新内容。" else "之后有新内容时会自动下载。")
                    Text("已下载的视频会保留，正在进行的下载也会继续。")
                }
                NasManagementError(error, "刷新查看", onRefresh)
            }
        },
        confirmButton = {
            TextButton(enabled = !busy, onClick = {
                if (!busy) {
                    busy = true; error = null; onBusyChange(true)
                    scope.launch {
                        try {
                            if (removing) {
                                client.removeTitle(title.id, deleteFiles, addImportExclusion)
                                onRemoved()
                            } else onUpdated(client.setMonitored(title.id, !title.monitored))
                        } catch (cancel: CancellationException) { throw cancel }
                        catch (problem: Exception) { error = safeError(problem) }
                        finally { busy = false; onBusyChange(false) }
                    }
                }
            }) {
                Text(if (busy) "正在提交…" else if (removing && deleteFiles) "移除并删除视频" else if (removing) "移除作品" else monitorLabel,
                    color = if (removing) ReadingColors.Error else MaterialTheme.colorScheme.primary)
            }
        },
        dismissButton = { TextButton(enabled = !busy, onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun NasManagementCheckbox(label: String, checked: Boolean, enabled: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).toggleable(value = checked, enabled = enabled, role = Role.Checkbox, onValueChange = onChange),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Checkbox(checked = checked, onCheckedChange = null, enabled = enabled)
        Text(label, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun NasManagementError(message: String?, refreshLabel: String, onRefresh: () -> Unit) {
    if (message != null) {
        Text(message, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.Error)
        Text("操作可能已经完成，请刷新后再决定是否重试。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
        TextButton(onClick = onRefresh, modifier = Modifier.heightIn(min = 48.dp)) { Text(refreshLabel) }
    }
}

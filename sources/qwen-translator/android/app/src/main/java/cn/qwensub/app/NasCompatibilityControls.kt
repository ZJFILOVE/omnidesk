package cn.qwensub.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.VolumeUp
import androidx.compose.material.icons.outlined.Download
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Explicit compatibility fallback; polling alone never changes the user's current playback. */
@Composable
internal fun NasCompatibilityControls(
    file: NasMediaFile,
    settings: ConnectionSettings,
    onReady: (NasMediaFile) -> Unit,
    actionLabel: String = "修复声音",
    onDownload: ((NasMediaFile) -> Unit)? = null,
) {
    if (!file.compatibleAvailable) return
    val client = remember(settings) { MediaClient(settings) }
    val scope = rememberCoroutineScope()
    val currentReady by rememberUpdatedState(onReady)
    val currentDownload by rememberUpdatedState(onDownload)
    var status by remember(file.id, settings.baseUrl) {
        mutableStateOf(file.compatible ?: NasMediaCompatibility("idle", 0f, ""))
    }
    var requested by remember(file.id, settings.baseUrl) { mutableStateOf<String?>(null) }
    var submitting by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(file.compatible) { file.compatible?.let { status = it } }
    LaunchedEffect(client, file.id, status.state) {
        if (!status.active) return@LaunchedEffect
        while (true) {
            delay(2_000)
            try {
                val updated = client.compatibility(file.id)
                status = updated
                error = null
                if (!updated.active) {
                    if (updated.ready && requested != null) {
                        val output = file.compatibleCopy(updated)
                        val action = requested
                        requested = null
                        if (action == "download") currentDownload?.invoke(output) else currentReady(output)
                    }
                    break
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { error = safeError(failure) }
        }
    }
    fun prepare(download: Boolean = false) {
        if (submitting) return
        scope.launch {
            submitting = true
            requested = if (download) "download" else "play"
            error = null
            try {
                // POST is idempotent for queued/running/ready variants and revalidates an evicted cache.
                val updated = client.prepareCompatible(file.id)
                status = updated
                if (updated.ready) {
                    val output = file.compatibleCopy(updated)
                    requested = null
                    if (download) currentDownload?.invoke(output) else currentReady(output)
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { error = safeError(failure) }
            finally { submitting = false }
        }
    }
    Surface(color = ReadingColors.Inset, shape = MaterialTheme.shapes.large) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("没有声音？", style = MaterialTheme.typography.titleSmall)
            Text("有些手机或耳机无法播放这段声音。可以生成一个兼容版本，画质不变，原视频也会保留。",
                style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
            if (status.active || submitting) {
                LinearProgressIndicator(progress = { status.progress }, modifier = Modifier.fillMaxWidth())
                Text(status.message.ifBlank { "正在修复声音…" }, style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                if (requested != null) {
                    Text(if (requested == "download") "修复后会自动保存到手机。" else "修复后会自动切换播放。", style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
                } else {
                    TextButton(onClick = { requested = "play" }, modifier = Modifier.heightIn(min = 48.dp)) { Text("修复后自动播放") }
                }
            } else {
                FilledTonalButton(onClick = { prepare() }, enabled = !submitting, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Icon(Icons.AutoMirrored.Outlined.VolumeUp, contentDescription = null, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text(if (status.state == "failed") "重新修复" else actionLabel)
                }
                if (onDownload != null) OutlinedButton(onClick = { prepare(download = true) }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Icon(Icons.Outlined.Download, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.width(8.dp)); Text(if (status.ready) "把修复版存到手机" else "修复并存到手机")
                }
            }
            if (status.state == "failed" && status.message.isNotBlank()) Text(status.message, style = MaterialTheme.typography.bodySmall, color = ReadingColors.Error)
            error?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = ReadingColors.Error) }
        }
    }
}

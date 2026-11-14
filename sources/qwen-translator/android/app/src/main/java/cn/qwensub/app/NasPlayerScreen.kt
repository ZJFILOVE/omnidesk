package cn.qwensub.app

import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.security.MessageDigest

/** NAS playback shares the local player; absent subtitles are simply skipped. */
@Composable
internal fun NasPlayerScreen(playback: NasPlayback, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val client = remember(playback.settings) { MediaClient(playback.settings) }
    val key = remember(playback.file.id, playback.settings.baseUrl) {
        MessageDigest.getInstance("SHA-256").digest("${playback.settings.baseUrl}/${playback.file.id}".toByteArray())
            .joinToString("") { "%02x".format(it) }
    }
    val preferences = remember { context.getSharedPreferences("nas-playback", Context.MODE_PRIVATE) }
    val requestStore = remember { MediaRequestStore(context) }
    var jobId by rememberSaveable(key) { mutableStateOf(if (playback.subtitles.isEmpty()) null else playback.jobId ?: playback.file.job?.id) }
    var job by remember(key) { mutableStateOf(playback.file.job?.takeIf { playback.subtitles.isNotEmpty() }) }
    var selected by rememberSaveable(key) { mutableStateOf(playback.selectedSubtitleId) }
    var jobSource by rememberSaveable(key) { mutableStateOf(preferences.getString("source_$key", null)) }
    var error by remember(key) { mutableStateOf<String?>(null) }
    var submitting by remember(key) { mutableStateOf(false) }
    var streamFile by remember(playback.file.id, playback.file.streamPath) {
        mutableStateOf(playback.file.compatible?.takeIf { it.ready }?.let { playback.file.compatibleCopy(it) } ?: playback.file)
    }

    fun prepare() {
        if (submitting) return
        val source = playback.subtitles.firstOrNull { it.id == selected && it.source != "generated" }
        val requestId = runCatching { requestStore.request(playback.settings, playback.file.id, source?.id, false, null) }
            .getOrElse { error = safeError(it); return }
        submitting = true
        error = null
        scope.launch {
            try {
                val remote = if (job?.state in setOf("failed", "cancelled") && jobId != null && jobSource == source?.id) client.retry(requireNotNull(jobId))
                else client.prepare(playback.file.id, subtitleId = source?.id, requestId = requestId)
                job = remote
                jobId = remote.id
                jobSource = source?.id
                preferences.edit().putString("job_$key", remote.id).putString("source_$key", source?.id).apply()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { error = safeError(failure) }
            finally { submitting = false }
        }
    }

    LaunchedEffect(jobId) {
        val id = jobId ?: return@LaunchedEffect
        while (true) {
            try {
                val current = client.job(id)
                job = current
                error = null
                if (current.finished) {
                    if (current.ready) selected = generatedMediaSubtitles(current.id).first().id
                    break
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { error = safeError(failure) }
            delay(5000)
        }
    }
    val generated = job?.takeIf { it.ready }?.let { generatedMediaSubtitles(it.id) }.orEmpty()
    val current = playback.copy(
        file = streamFile.copy(job = job),
        subtitles = (generated + playback.subtitles).distinctBy { it.downloadPath },
        selectedSubtitleId = selected,
        jobId = jobId, clientRequestId = job?.clientRequestId ?: playback.clientRequestId,
    )
    val task = remember(key) { DownloadTask(id = "nas-$key", title = playback.file.title, url = "", videoState = TaskState.READY) }
    PlayerScreen(task, onBack, nas = current, onNasSubtitleSelected = { selected = it }, playbackActions = {
        if (streamFile.streamPath.endsWith("/compatible/stream")) {
            Text("正在使用修复后的声音，画质不变", style = MaterialTheme.typography.bodySmall, color = ReadingColors.LocalTeal)
        } else NasCompatibilityControls(streamFile, playback.settings, onReady = { streamFile = it })
    }, subtitleActions = {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (playback.subtitles.isEmpty()) "这个视频暂时没有字幕，仍可正常观看。" else "可以选择现有字幕，或生成中文字幕。",
                style = MaterialTheme.typography.bodySmall, color = ReadingColors.SecondaryInk)
            val active = submitting || job?.finished == false
            if (active) {
                LinearProgressIndicator(progress = { job?.progress ?: 0f }, modifier = Modifier.fillMaxWidth())
                Text(job?.message?.ifBlank { "正在生成字幕" } ?: "正在开始生成字幕", style = MaterialTheme.typography.bodySmall)
                TextButton(enabled = !submitting, onClick = { scope.launch {
                    try { jobId?.let { client.cancel(it); job = client.job(it) } }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (failure: Exception) { error = safeError(failure) }
                } }) { Text("停止生成") }
            } else if (playback.subtitles.any { it.source != "generated" }) {
                Button(onClick = { prepare() }) {
                    Text(if (job?.state in setOf("failed", "cancelled")) "重试字幕" else "生成中文字幕")
                }
            }
            if (job?.state == "failed" || job?.state == "cancelled") Text(job?.message.orEmpty(),
                style = MaterialTheme.typography.bodySmall, color = ReadingColors.Error)
            error?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = ReadingColors.Error) }
        }
    })
}

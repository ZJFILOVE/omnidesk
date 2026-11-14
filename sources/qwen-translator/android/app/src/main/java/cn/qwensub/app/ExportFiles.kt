package cn.qwensub.app

import android.content.Context
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** The system file picker owns the destination; no broad storage permission is needed. */
internal suspend fun writeDiagnostics(context: Context, uri: Uri, report: String) = withContext(Dispatchers.IO) {
    val stream = context.contentResolver.openOutputStream(uri, "wt")
        ?: error("无法打开所选文件，请重新选择保存位置。")
    stream.bufferedWriter(Charsets.UTF_8).use { it.write(report) }
}

internal fun playbackDiagnostics(context: Context): String {
    val preferences = context.getSharedPreferences("playback", Context.MODE_PRIVATE)
    return buildString {
        appendLine("播放器诊断")
        appendLine("最近任务: ${preferences.getString("last_task", "无")}")
        appendLine("最近错误时间: ${preferences.getLong("last_error_at", 0)}")
        appendLine("最近错误代码: ${preferences.getString("last_error_code", "无")}")
    }
}

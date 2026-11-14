package cn.qwensub.app

import android.content.Context
import java.security.MessageDigest

/** Choices belong to one NAS and service; secrets and media paths are never saved here. */
internal class MediaSelectionSettings(context: Context, server: String, kind: String) {
    private val preferences = context.getSharedPreferences("media_selection", Context.MODE_PRIVATE)
    private val prefix = MessageDigest.getInstance("SHA-256")
        .digest((server.trim().trimEnd('/') + "\n" + kind).toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    fun quality(): Int? = preferences.getInt("$prefix:quality", 0).takeIf { it > 0 }
    fun root(): Int? = preferences.getInt("$prefix:root", 0).takeIf { it > 0 }
    fun saveQuality(id: Int) { preferences.edit().putInt("$prefix:quality", id).apply() }
    fun saveRoot(id: Int) { preferences.edit().putInt("$prefix:root", id).apply() }
}

internal fun resolveMediaSelection(options: List<NasMediaOption>, saved: Int?, recommended: Int? = null): Int? =
    saved?.takeIf { id -> options.any { it.id == id } }
        ?: recommended?.takeIf { id -> options.any { it.id == id } }
        ?: options.singleOrNull()?.id

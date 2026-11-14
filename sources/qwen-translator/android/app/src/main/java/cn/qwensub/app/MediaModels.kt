package cn.qwensub.app

/** Library IDs are opaque service identifiers, never NAS filesystem paths. */
data class NasMediaTitle(
    val id: String,
    val kind: String,
    val title: String,
    val year: Int?,
    val overview: String,
    val monitored: Boolean,
    val availableCount: Int,
    val totalCount: Int,
    val mediaId: String?,
    val episodes: List<NasMediaEpisode> = emptyList(),
    val searchMessage: String? = null,
)

data class NasMediaEpisode(
    val id: String,
    val title: String,
    val seasonNumber: Int,
    val episodeNumber: Int,
    val hasFile: Boolean,
    val monitored: Boolean,
    val mediaId: String?,
) {
    val episodeLabel: String get() = "S%02dE%02d".format(java.util.Locale.ROOT, seasonNumber, episodeNumber)
}

data class NasMediaService(val kind: String, val connected: Boolean, val message: String)
data class NasMediaCatalog(
    val configured: Boolean,
    val items: List<NasMediaTitle>,
    val services: List<NasMediaService>,
    val capabilities: Set<String> = emptySet(),
)

data class NasMediaQueueItem(
    val id: String, val title: String, val status: String, val progress: Float,
    val size: Long, val sizeLeft: Long, val timeLeft: String, val kind: String,
)

data class NasMediaSubtitle(
    val id: String,
    val title: String,
    val language: String,
    val source: String,
    val downloadPath: String,
)

data class NasMediaAudio(val id: Int, val title: String, val language: String)

data class NasMediaFile(
    val id: String,
    val title: String,
    val filename: String,
    val size: Long,
    val streamPath: String,
    val subtitles: List<NasMediaSubtitle>,
    val duration: Double,
    val audio: List<NasMediaAudio>,
    val job: NasMediaJob? = null,
    val asrAvailable: Boolean = false,
    val compatible: NasMediaCompatibility? = null,
    val compatibleAvailable: Boolean = false,
    val compatibleStreamPath: String? = null,
)

data class NasMediaJob(
    val id: String,
    val state: String,
    val progress: Float,
    val message: String,
    val model: String,
    val mediaId: String,
    val clientRequestId: String? = null,
) {
    val finished: Boolean get() = state in setOf("ready", "failed", "cancelled")
    val ready: Boolean get() = state == "ready"
}

/** Captures the connection and selected captions for both streaming and an offline download. */
data class NasPlayback(
    val file: NasMediaFile,
    val subtitles: List<NasMediaSubtitle> = file.subtitles,
    val selectedSubtitleId: String? = null,
    val settings: ConnectionSettings,
    val jobId: String? = null,
    val clientRequestId: String? = null,
)

internal fun generatedMediaSubtitles(jobId: String): List<NasMediaSubtitle> = listOf(
    NasMediaSubtitle("generated-${jobId}-bilingual", "双语字幕", "zh", "generated", "/v1/media/jobs/$jobId/subtitles/bilingual"),
    NasMediaSubtitle("generated-${jobId}-zh", "中文字幕", "zh", "generated", "/v1/media/jobs/$jobId/subtitles/zh"),
    NasMediaSubtitle("generated-${jobId}-original", "原文字幕", "und", "generated", "/v1/media/jobs/$jobId/subtitles/original"),
)


data class NasDiscoveredTitle(
    val externalId: Int, val kind: String, val title: String, val year: Int?,
    val overview: String, val alreadyAdded: Boolean, val titleId: String?,
)
data class NasMediaOption(val id: Int, val name: String, val description: String = "")
data class NasMediaAddOptions(
    val rootFolders: List<NasMediaOption>, val qualityProfiles: List<NasMediaOption>,
    val defaultQualityProfileId: Int? = null,
)


/** A video-copy / AAC-audio repack; source media and original subtitle routes remain unchanged. */
data class NasMediaCompatibility(
    val state: String,
    val progress: Float,
    val message: String,
    val streamPath: String? = null,
    val size: Long? = null,
) {
    val ready: Boolean get() = state == "ready" && streamPath != null
    val active: Boolean get() = state in setOf("queued", "running")
}

internal fun NasMediaFile.compatibleCopy(status: NasMediaCompatibility): NasMediaFile {
    require(status.ready) { "兼容版本尚未准备好。" }
    return copy(streamPath = requireNotNull(status.streamPath), size = status.size ?: size,
        filename = filename.substringBeforeLast('.', filename) + ".mkv", compatible = status,
        compatibleStreamPath = status.streamPath)
}

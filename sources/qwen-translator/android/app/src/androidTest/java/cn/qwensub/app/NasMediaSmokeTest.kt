package cn.qwensub.app

import android.graphics.SurfaceTexture
import android.view.Surface
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.text.CueGroup
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.analytics.AnalyticsListener
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.Proxy
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Explicit read-only live test. Never starts Arr searches, AI jobs, downloads or cleanup. */
@RunWith(AndroidJUnit4::class)
@androidx.annotation.OptIn(UnstableApi::class)
class NasMediaSmokeTest {
    @Test(timeout = 150_000) fun browseAndStreamNasWithSubtitles() = runBlocking {
        assumeTrue("Pass -e nasMediaSmoke true to test the configured NAS",
            InstrumentationRegistry.getArguments().getString("nasMediaSmoke") == "true")
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        val settings = AppRepository.get(context).settings()
        val client = MediaClient(settings)
        val phase = File(context.filesDir, "nas-media-phase.txt")
        phase.writeText("health")
        assertTrue(NasClient(settings).health().contains("连接成功"))
        phase.writeText("catalog")
        val catalog = client.catalog()
        assertTrue("Media service is not configured", catalog.configured)
        assertTrue("No connected Arr services", catalog.services.any { it.connected })
        val title = catalog.items.firstOrNull { it.availableCount > 0 } ?: error("No downloaded media to verify")
        phase.writeText("episodes")
        val mediaId = title.mediaId ?: client.title(title.id).episodes.first { it.hasFile && it.mediaId != null }.mediaId!!
        phase.writeText("file")
        val media = client.file(mediaId)
        val compatible = InstrumentationRegistry.getArguments().getString("compatibleNasMedia") == "true"
        val stream = if (compatible) media.compatible?.takeIf { it.ready }?.let(media::compatibleCopy)
            ?: error("Prepare the compatibility variant before this playback test") else media
        assertTrue("No discovered subtitle choices", media.subtitles.isNotEmpty())
        val subtitle = media.subtitles.firstOrNull { it.downloadPath.endsWith("/subtitles/bilingual") } ?: media.subtitles.first()
        phase.writeText("subtitle")
        val srt = client.text(subtitle.downloadPath)
        assertTrue("Subtitle endpoint did not return SRT", srt.contains("-->"))
        val firstTime = Regex("(?m)^(\\d{2,3}):(\\d{2}):(\\d{2}),(\\d{3}) -->").find(srt)?.groupValues
            ?: error("No subtitle timestamps")
        val cueStart = ((firstTime[1].toLong() * 60 + firstTime[2].toLong()) * 60 + firstTime[3].toLong()) * 1000 + firstTime[4].toLong()
        val chineseCaptions = subtitle.downloadPath.endsWith("/subtitles/bilingual")
        phase.writeText("queue")
        client.queue()
        phase.writeText("playback")

        val video = CountDownLatch(1)
        val audio = CountDownLatch(1)
        val captions = CountDownLatch(1)
        val failure = AtomicReference<String?>()
        var player: ExoPlayer? = null
        var surface: Surface? = null
        var texture: SurfaceTexture? = null
        instrumentation.runOnMainSync {
            texture = SurfaceTexture(0)
            surface = Surface(texture)
            val http = OkHttpClient.Builder().proxy(Proxy.NO_PROXY).followRedirects(false).followSslRedirects(false).build()
            val source = OkHttpDataSource.Factory(http).setDefaultRequestProperties(mapOf("Authorization" to "Bearer ${settings.token}"))
            player = ExoPlayer.Builder(context).setMediaSourceFactory(DefaultMediaSourceFactory(DefaultDataSource.Factory(context, source))
                .setSubtitleParserFactory(PlaybackSubtitleParserFactory())).build().apply {
                volume = 0f
                setVideoSurface(surface)
                addAnalyticsListener(object : AnalyticsListener {
                    override fun onAudioPositionAdvancing(eventTime: AnalyticsListener.EventTime, playoutStartSystemTimeMs: Long) { audio.countDown() }
                })
                addListener(object : Player.Listener {
                    override fun onRenderedFirstFrame() { video.countDown() }
                    override fun onCues(cueGroup: CueGroup) {
                        if (cueGroup.cues.any { !chineseCaptions || Regex("[\\u3400-\\u9fff]").containsMatchIn(it.text?.toString().orEmpty()) }) captions.countDown()
                    }
                    override fun onPlayerError(error: PlaybackException) {
                        failure.set(error.errorCodeName)
                        video.countDown(); audio.countDown(); captions.countDown()
                    }
                })
                val textLanguage = if (chineseCaptions) "zh" else subtitle.language
                trackSelectionParameters = trackSelectionParameters.buildUpon().setTrackTypeDisabled(C.TRACK_TYPE_TEXT, false)
                    .setPreferredTextLanguage(textLanguage).build()
                setMediaItem(MediaItem.Builder().setUri(client.resolvePath(stream.streamPath))
                    .setSubtitleConfigurations(listOf(MediaItem.SubtitleConfiguration.Builder(android.net.Uri.parse(client.resolvePath(subtitle.downloadPath)))
                        .setId(subtitle.id).setMimeType(MimeTypes.APPLICATION_SUBRIP).setLanguage(textLanguage)
                        .setSelectionFlags(C.SELECTION_FLAG_DEFAULT).build())).build(), (cueStart - 500).coerceAtLeast(0))
                prepare(); play()
            }
        }
        try {
            assertTrue("No NAS video frame rendered", video.await(45, TimeUnit.SECONDS)); assertNull(failure.get())
            assertTrue("No decoded NAS audio", audio.await(20, TimeUnit.SECONDS)); assertNull(failure.get())
            assertTrue("No NAS subtitle cues", captions.await(30, TimeUnit.SECONDS)); assertNull(failure.get())
            File(context.filesDir, "nas-media-smoke.json").writeText(JSONObject().apply {
                put("catalog", true); put("series_or_movie", true); put("subtitles", true)
                put("video_frame", true); put("audio_decoded", true); put("subtitle_cues", true)
                put("compatible_audio", compatible)
                put("chinese_captions", chineseCaptions)
            }.toString())
        } finally {
            instrumentation.runOnMainSync { player?.release(); surface?.release(); texture?.release() }
        }
    }
}

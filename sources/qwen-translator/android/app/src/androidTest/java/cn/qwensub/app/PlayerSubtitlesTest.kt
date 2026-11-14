package cn.qwensub.app

import android.net.Uri
import android.view.View
import androidx.media3.common.C
import androidx.media3.common.Format
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.text.CueGroup
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.extractor.text.CuesWithTiming
import androidx.media3.extractor.text.DefaultSubtitleParserFactory
import androidx.media3.extractor.text.SubtitleParser
import androidx.media3.ui.PlayerView
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

@RunWith(AndroidJUnit4::class)
@androidx.annotation.OptIn(UnstableApi::class)
class PlayerSubtitlesTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private val srt = """
        1
        00:00:00,000 --> 00:00:03,000
        第一条字幕
        First caption

        2
        00:00:01,000 --> 00:00:04,000
        第二条字幕
        Second caption

        3
        00:00:04,500 --> 00:00:05,500
        第三条字幕
        Third caption

    """.trimIndent().toByteArray()

    @Test fun parserPreservesBilingualTextAndRemovesOverlap() {
        val format = Format.Builder().setSampleMimeType(MimeTypes.APPLICATION_SUBRIP).build()
        val factory = PlaybackSubtitleParserFactory()
        val parser = factory.create(format)
        val cues = mutableListOf<CuesWithTiming>()
        parser.parse(srt, SubtitleParser.OutputOptions.allCues()) { cues.add(it) }
        assertEquals(4, cues.size)
        assertTrue(cues.last().cues.isEmpty())
        assertEquals(5_500_000L, cues.last().startTimeUs)
        assertEquals(1_000_000L, cues[0].durationUs)
        assertEquals("第二条字幕\nSecond caption", cues[1].cues.single().text.toString())
        assertEquals(1_000_000L, cues[1].startTimeUs)
        assertEquals(3_000_000L, cues[1].durationUs)
        assertEquals(factory.getCueReplacementBehavior(format), parser.cueReplacementBehavior)
        val vtt = Format.Builder().setSampleMimeType(MimeTypes.TEXT_VTT).build()
        assertEquals(DefaultSubtitleParserFactory().getCueReplacementBehavior(vtt), factory.getCueReplacementBehavior(vtt))

        val resumed = mutableListOf<CuesWithTiming>()
        parser.reset()
        parser.parse(srt, SubtitleParser.OutputOptions.cuesAfterThenRemainingCuesBefore(4_750_000L)) { resumed.add(it) }
        assertEquals(4_750_000L, resumed.first().startTimeUs)
        assertEquals(750_000L, resumed.first().durationUs)
        assertEquals("第三条字幕\nThird caption", resumed.first().cues.single().text.toString())
        val chronological = resumed.sortedBy { it.startTimeUs }
        assertTrue(chronological.zipWithNext().all { (a, b) -> a.endTimeUs <= b.startTimeUs })
    }

    @Test fun overlappingCaptionsReplaceEachOtherDuringPlaybackAndSeeking() {
        val audio = File.createTempFile("subtitle-playback-", ".wav", context.cacheDir)
        val subtitles = File.createTempFile("subtitle-playback-", ".srt", context.cacheDir)
        val failure = AtomicReference<String?>()
        val captions = mutableSetOf<String>()
        var sawGap = false
        val finished = CountDownLatch(1)
        lateinit var player: ExoPlayer
        var playerForRelease: ExoPlayer? = null
        try {
            audio.writeBytes(silentWav())
            subtitles.writeBytes(srt)
            instrumentation.runOnMainSync {
                player = ExoPlayer.Builder(context).setMediaSourceFactory(
                    DefaultMediaSourceFactory(context).setSubtitleParserFactory(PlaybackSubtitleParserFactory())
                ).build().also { playerForRelease = it }.apply {
                    volume = 0f
                    addListener(object : Player.Listener {
                        override fun onCues(cueGroup: CueGroup) {
                            if (cueGroup.cues.size > 1) failure.set("Overlapping subtitle boxes were rendered")
                            cueGroup.cues.forEach { captions.add(it.text.toString()) }
                            if (currentPosition >= 3_900 && cueGroup.cues.isEmpty()) sawGap = true
                            if (cueGroup.cues.any { it.text.toString().contains("Third caption") }) finished.countDown()
                        }
                        override fun onPlayerError(error: PlaybackException) {
                            failure.set(error.errorCodeName)
                            finished.countDown()
                        }
                    })
                    setMediaItem(MediaItem.Builder().setUri(Uri.fromFile(audio)).setSubtitleConfigurations(listOf(
                        MediaItem.SubtitleConfiguration.Builder(Uri.fromFile(subtitles))
                            .setMimeType(MimeTypes.APPLICATION_SUBRIP).setLanguage("zh")
                            .setSelectionFlags(C.SELECTION_FLAG_DEFAULT).build()
                    )).build())
                    prepare()
                    play()
                }
            }
            assertTrue("Playback did not reach the third caption", finished.await(15, TimeUnit.SECONDS))
            instrumentation.runOnMainSync {
                player.pause()
                assertNull(failure.get())
                assertEquals(3, captions.size)
                assertTrue("Expired subtitles remained visible in the gap", sawGap)
            }
            for ((position, expected) in listOf(500L to "First caption", 1500L to "Second caption", 4750L to "Third caption")) {
                val reached = CountDownLatch(1)
                val history = mutableListOf<String>()
                val listener = object : Player.Listener {
                    override fun onCues(cueGroup: CueGroup) {
                        history.add("${player.currentPosition}: ${cueGroup.cues.map { it.text.toString() }}")
                        if (cueGroup.cues.singleOrNull()?.text.toString().contains(expected)) reached.countDown()
                    }
                }
                instrumentation.runOnMainSync { player.addListener(listener); player.seekTo(position); player.play() }
                val matched = reached.await(10, TimeUnit.SECONDS)
                instrumentation.runOnMainSync {
                    player.pause(); player.removeListener(listener); assertNull(failure.get())
                    assertTrue("Wrong subtitle after seeking to $position (state=${player.playbackState}, position=${player.currentPosition}, events=$history)", matched)
                }
            }
        } finally {
            instrumentation.runOnMainSync { playerForRelease?.release() }
            audio.delete()
            subtitles.delete()
        }
    }

    @Test fun controlsReserveSpaceInPortraitAndFullscreen() {
        instrumentation.runOnMainSync {
            val view = PlayerView(context)
            for ((width, height, fullscreen) in listOf(Triple(1080, 608, false), Triple(1920, 1080, true))) {
                view.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY))
                view.layout(0, 0, width, height)
                view.updateSubtitleLayout(fullscreen, controlsVisible = true)
                val subtitles = view.subtitleView!!
                val bottomBar = view.findViewById<View>(androidx.media3.ui.R.id.exo_bottom_bar)
                val progress = view.findViewById<View>(androidx.media3.ui.R.id.exo_progress)
                assertTrue("Subtitles overlap the bottom control bar or seek handle",
                    subtitles.paddingBottom > bottomBar.height + progress.height)
                val visiblePadding = subtitles.paddingBottom
                view.updateSubtitleLayout(fullscreen, controlsVisible = false)
                assertTrue("Hidden controls still consume subtitle space", subtitles.paddingBottom < visiblePadding)
                assertTrue("No horizontal safe margin", subtitles.paddingLeft > 0)
            }
        }
    }

    private fun silentWav(): ByteArray {
        val samples = ByteArray(8000 * 2 * 6)
        return ByteBuffer.allocate(44 + samples.size).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray()); putInt(36 + samples.size); put("WAVEfmt ".toByteArray())
            putInt(16); putShort(1); putShort(1); putInt(8000); putInt(16000); putShort(2); putShort(16)
            put("data".toByteArray()); putInt(samples.size); put(samples)
        }.array()
    }
}

package cn.qwensub.app

import android.util.TypedValue
import android.view.View
import androidx.media3.common.C
import androidx.media3.common.Format
import androidx.media3.common.MimeTypes
import androidx.media3.common.util.UnstableApi
import androidx.media3.common.util.Consumer
import androidx.media3.extractor.text.CuesWithTiming
import androidx.media3.extractor.text.DefaultSubtitleParserFactory
import androidx.media3.extractor.text.SubtitleParser
import androidx.media3.ui.PlayerView
import kotlin.math.min
import kotlin.math.roundToInt

/** YouTube SRT cues retain the previous rolling line's end time. Show the newest pair. */
@androidx.annotation.OptIn(UnstableApi::class)
internal class PlaybackSubtitleParserFactory(
    private val delegate: SubtitleParser.Factory = DefaultSubtitleParserFactory(),
) : SubtitleParser.Factory by delegate {
    override fun create(format: Format): SubtitleParser {
        val parser = delegate.create(format)
        if (format.sampleMimeType != MimeTypes.APPLICATION_SUBRIP) return parser
        return object : SubtitleParser by parser {
            override fun parse(data: ByteArray, offset: Int, length: Int,
                outputOptions: SubtitleParser.OutputOptions, output: Consumer<CuesWithTiming>) {
                val cues = mutableListOf<CuesWithTiming>()
                parser.parse(data, offset, length, SubtitleParser.OutputOptions.allCues()) { cues.add(it) }
                cues.sortBy { it.startTimeUs }
                val beforeSeek = mutableListOf<CuesWithTiming>()
                fun emit(cue: CuesWithTiming) {
                    if (outputOptions.startTimeUs == C.TIME_UNSET || cue.startTimeUs >= outputOptions.startTimeUs) {
                        output.accept(cue)
                    } else if (outputOptions.outputAllCues) beforeSeek.add(cue)
                }
                cues.forEachIndexed { index, cue ->
                    val end = min(cue.endTimeUs, cues.getOrNull(index + 1)?.startTimeUs ?: cue.endTimeUs)
                    if (end <= cue.startTimeUs) return@forEachIndexed
                    val seek = outputOptions.startTimeUs
                    if (seek > cue.startTimeUs && seek < end) {
                        // Media3 writes only samples starting at/after the initial seek. Split the
                        // active cue so it appears immediately, retaining the prefix for later seeks.
                        emit(CuesWithTiming(cue.cues, cue.startTimeUs, seek - cue.startTimeUs))
                        emit(CuesWithTiming(cue.cues, seek, end - seek))
                    } else emit(CuesWithTiming(cue.cues, cue.startTimeUs, end - cue.startTimeUs))
                }
                // Include the final end in Media3's sample buffer; otherwise a seek into the
                // final caption can advance past the last sample even while that cue is active.
                cues.lastOrNull()?.let { emit(CuesWithTiming(emptyList(), it.endTimeUs, 0)) }
                beforeSeek.forEach(output::accept)
            }
        }
    }
}

/** Size text against the video viewport, independently of the space reserved for controls. */
@androidx.annotation.OptIn(UnstableApi::class)
internal fun PlayerView.updateSubtitleLayout(fullscreen: Boolean, controlsVisible: Boolean) {
    val subtitles = subtitleView ?: return
    if (subtitles.height <= 0) return
    val metrics = resources.displayMetrics
    val textSize = min(subtitles.height * if (fullscreen) 0.042f else 0.052f,
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, if (fullscreen) 20f else 14f, metrics))
    subtitles.setFixedTextSize(TypedValue.COMPLEX_UNIT_PX, textSize)
    val margin = (8 * metrics.density).roundToInt()
    val controls = findViewById<View>(androidx.media3.ui.R.id.exo_bottom_bar)
    val progress = findViewById<View>(androidx.media3.ui.R.id.exo_progress)
    val controlHeight = if (controlsVisible) maxOf((controls?.height ?: 0) + (progress?.height ?: 0),
        (72 * metrics.density).roundToInt()) else 0
    subtitles.setPadding(margin * 2, 0, margin * 2, controlHeight + margin)
    subtitles.setBottomPaddingFraction(0.04f)
}

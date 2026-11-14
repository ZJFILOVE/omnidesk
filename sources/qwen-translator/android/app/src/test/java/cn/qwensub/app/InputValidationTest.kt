package cn.qwensub.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Test

class InputValidationTest {
    @Test fun acceptsOneVideoAndRemovesPlaylistTrackingAndStartTime() {
        val expected = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        listOf("https://youtu.be/jNQXAC9IVRw?t=5", "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=abc",
            "https://m.youtube.com/shorts/jNQXAC9IVRw", "https://youtube.com/live/jNQXAC9IVRw")
            .forEach { assertEquals(expected, InputValidation.youtubeUrl(it)) }
    }

    @Test fun rejectsArgumentInjectionNonYouTubeAndPlaylistOnlyLinks() {
        listOf("--exec sh", "file:///etc/passwd", "https://youtube.com.evil.test/watch?v=jNQXAC9IVRw",
            "https://youtube.com@evil.test/watch?v=jNQXAC9IVRw", "https://youtube.com/playlist?list=abc",
            "https://youtu.be/not-valid", "https://youtube.com:8443/watch?v=jNQXAC9IVRw").forEach {
            assertThrows(IllegalArgumentException::class.java) { InputValidation.youtubeUrl(it) }
        }
    }

    @Test fun allowsLocalUsbLanAndTailscaleHttpButRequiresTlsForPublicNas() {
        listOf("http://127.0.0.1:18765", "http://192.168.2.8:8765", "http://100.100.2.8:8765", "https://nas.example.ts.net")
            .forEach { assertEquals(it, InputValidation.settings(ConnectionSettings(it)).baseUrl) }
        listOf("http://example.com", "http://100.63.2.8", "http://172.32.0.1", "http://192.169.0.1", "https://user:pass@example.com", "https://example.com/?secret=x")
            .forEach { assertThrows(IllegalArgumentException::class.java) { InputValidation.settings(ConnectionSettings(it)) } }
    }

    @Test fun rejectsCredentialsContainingHeaderBreaksAndBadProxyUrls() {
        assertThrows(IllegalArgumentException::class.java) {
            InputValidation.settings(ConnectionSettings(token = "abc\r\nX: injected"))
        }
        listOf("localhost:7890", "http://localhost", "file:///tmp/proxy", "https://proxy:7890/path")
            .forEach { assertThrows(IllegalArgumentException::class.java) { InputValidation.settings(ConnectionSettings(downloadProxy = it)) } }
        assertEquals("http://127.0.0.1:17897", InputValidation.settings(ConnectionSettings(downloadProxy = "http://127.0.0.1:17897")).downloadProxy)
    }

    @Test fun diagnosticsErrorRedactsBearerUrlsAndPrivateFilePaths() {
        val safe = safeError(IllegalStateException("ERROR: Bearer abc.def https://secret:token@host/path /data/user/0/private/file"))
        assertFalse(safe.contains("abc.def")); assertFalse(safe.contains("secret")); assertFalse(safe.contains("/data/"))
    }

    @Test fun normalizesFfmpegSubtitlesSortsRenumbersAndDropsEmptyTimingMarkers() {
        val source = "8\r\n00:00:03.000 --> 00:00:04.000\r\nWorld\r\n\r\n2\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n3\r\n00:00:02,000 --> 00:00:02,000\r\nmarker"
        val normalized = SrtNormalization.normalize(source)
        assertEquals("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld\n\n", normalized)
        assertEquals(normalized, SrtNormalization.normalize(normalized))
    }

    @Test fun refusesMalformedSrtRatherThanUploadingPartialText() {
        listOf("1\n00:99:00,000 --> 00:99:01,000\nHello", "1\ninvalid --> 00:00:02,000\nHello",
            "1\n00:00:01,000 --> 00:00:02,000\nHello\u0000").forEach {
            assertThrows(IllegalArgumentException::class.java) { SrtNormalization.normalize(it) }
        }
    }

    @Test fun rejectsOversizedSubtitlesBeforeSendingToNas() {
        assertThrows(IllegalArgumentException::class.java) { SrtNormalization.normalize("中".repeat(700000)) }
    }
}

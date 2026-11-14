package cn.qwensub.app

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

class BundledDownloaderTest {
    @get:Rule val temporary = TemporaryFolder()
    private val bundle get() = File("src/main/assets/${BundledDownloader.ASSET}")

    private fun installedCore(version: String): File = temporary.newFile("yt-dlp").also { file ->
        ZipOutputStream(file.outputStream()).use { archive ->
            archive.putNextEntry(ZipEntry("yt_dlp/version.py"))
            archive.write("__version__ = '$version'\n".toByteArray())
            archive.closeEntry()
        }
    }

    @Test fun replacesOldCoreWithVerifiedReleaseAndSkipsRewritingIt() {
        val core = installedCore("2025.11.12")
        BundledDownloader.install(core) { bundle.inputStream() }
        assertEquals(BundledDownloader.VERSION, BundledDownloader.version(core))
        assertArrayEquals(bundle.readBytes(), core.readBytes())
        BundledDownloader.install(core) { error("Verified core must not be rewritten") }
    }

    @Test fun invalidBundleLeavesExistingCoreIntact() {
        val core = installedCore("2025.11.12")
        val before = core.readBytes()
        assertThrows(IllegalStateException::class.java) {
            BundledDownloader.install(core) { ByteArrayInputStream("corrupt update".toByteArray()) }
        }
        assertArrayEquals(before, core.readBytes())
        assertEquals(listOf("yt-dlp"), core.parentFile!!.list()!!.sorted())
    }

    @Test fun interruptedCopyLeavesExistingCoreIntact() {
        val core = installedCore("2025.11.12")
        val before = core.readBytes()
        assertThrows(IOException::class.java) {
            BundledDownloader.install(core) { object : InputStream() {
                override fun read(): Int = throw IOException("Interrupted copy")
            } }
        }
        assertArrayEquals(before, core.readBytes())
        assertEquals(listOf("yt-dlp"), core.parentFile!!.list()!!.sorted())
    }

    @Test fun keepsNewerInstalledReleases() {
        val core = installedCore("2026.10.01")
        BundledDownloader.install(core) { error("Newer core must not be downgraded") }
        assertEquals("2026.10.01", BundledDownloader.version(core))
    }

    @Test fun keepsNewerNightlyReleases() {
        val core = installedCore("2026.10.13.232839")
        BundledDownloader.install(core) { error("Newer nightly core must not be downgraded") }
        assertEquals("2026.10.13.232839", BundledDownloader.version(core))
    }
}

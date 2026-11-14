package cn.qwensub.app

import org.junit.Assert.*
import org.junit.Test

class MediaSelectionSettingsTest {
    private val options = listOf(NasMediaOption(1, "Any"), NasMediaOption(7, "做种最多优先 · 排除枪版"))

    @Test fun savedChoiceOverridesRecommendationButDeletedChoicesDoNot() {
        assertEquals(1, resolveMediaSelection(options, 1, 7))
        assertEquals(7, resolveMediaSelection(options, 99, 7))
        assertEquals(7, resolveMediaSelection(options, null, 7))
        assertNull(resolveMediaSelection(options, 99, 98))
        assertNull(resolveMediaSelection(emptyList(), 1, 7))
        assertEquals(7, resolveMediaSelection(options.takeLast(1), 99, 98))
    }
}

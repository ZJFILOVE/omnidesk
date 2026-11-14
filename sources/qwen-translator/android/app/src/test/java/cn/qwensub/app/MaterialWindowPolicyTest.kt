package cn.qwensub.app

import org.junit.Assert.*
import org.junit.Test

class MaterialWindowPolicyTest {
    @Test fun navigationFollowsAvailableWindowInsteadOfDeviceModel() {
        assertEquals(MaterialNavigation.BOTTOM, materialNavigation(393f))
        assertEquals(MaterialNavigation.BOTTOM, materialNavigation(599.9f))
        assertEquals(MaterialNavigation.RAIL, materialNavigation(600f))
        assertEquals(MaterialNavigation.RAIL, materialNavigation(839.9f))
        assertEquals(MaterialNavigation.DRAWER, materialNavigation(840f))
    }

    @Test fun splitScreenAndShortLandscapeKeepReadableSinglePane() {
        assertFalse(materialTwoPane(800f, 1200f))
        assertFalse(materialTwoPane(1000f, 420f))
        assertTrue(materialTwoPane(900f, 520f))
        assertTrue(materialTwoPane(1200f, 700f))
    }

    @Test fun draggedOrRestoredWidthCannotHideEitherPane() {
        assertEquals(0.32f, materialPaneFraction(-1f))
        assertEquals(0.58f, materialPaneFraction(100f))
        assertEquals(0.44f, materialPaneFraction(Float.NaN))
        assertEquals(0.44f, materialPaneFraction(Float.POSITIVE_INFINITY))
    }
}

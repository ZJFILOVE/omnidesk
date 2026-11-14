package cn.qwensub.app

internal enum class MaterialNavigation { BOTTOM, RAIL, DRAWER }

internal fun materialNavigation(widthDp: Float): MaterialNavigation = when {
    widthDp < MaterialTokens.LayoutCompactMax.value + 1f -> MaterialNavigation.BOTTOM
    widthDp < MaterialTokens.LayoutMediumMax.value + 1f -> MaterialNavigation.RAIL
    else -> MaterialNavigation.DRAWER
}

/** The available content area matters; a wide, short phone remains single-pane. */
internal fun materialTwoPane(widthDp: Float, heightDp: Float): Boolean = widthDp >= 900f && heightDp >= 520f

internal fun materialPaneFraction(value: Float): Float = if (value.isFinite()) value.coerceIn(0.32f, 0.58f) else 0.44f

package cn.qwensub.app

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

/** Shared by phone and tablet; the platform retains its native focus and ripple behavior. */
@Composable
internal fun MaterialPanel(modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    Surface(
        modifier = modifier,
        color = ReadingColors.Sheet,
        shape = MaterialTheme.shapes.large,
        border = BorderStroke(1.dp, ReadingColors.Border.copy(alpha = 0.55f)),
        shadowElevation = 1.dp,
        content = content,
    )
}

@Composable
internal fun FriendlyPageHeader(title: String, description: String, modifier: Modifier = Modifier) {
    Column(modifier, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(title, style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
        Text(description, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
    }
}

@Composable
internal fun MaterialSectionTitle(title: String, description: String) {
    Column(verticalArrangement = Arrangement.spacedBy(MaterialTokens.SpaceCompact)) {
        Text(title, style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
        Text(description, style = MaterialTheme.typography.bodyMedium, color = ReadingColors.SecondaryInk)
    }
}

@Composable
internal fun MaterialFilterChip(
    selected: Boolean, onClick: () -> Unit, label: @Composable () -> Unit,
    modifier: Modifier = Modifier, enabled: Boolean = true,
) {
    FilterChip(selected = selected, onClick = onClick, label = label, modifier = modifier, enabled = enabled,
        colors = FilterChipDefaults.filterChipColors(selectedContainerColor = ReadingColors.BlueWash,
            selectedLabelColor = ReadingColors.CaptionBlue))
}

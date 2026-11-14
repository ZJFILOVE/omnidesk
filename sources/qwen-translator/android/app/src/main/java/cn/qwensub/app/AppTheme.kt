package cn.qwensub.app

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

internal object ReadingColors {
    val Paper: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.background
    val Sheet: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.surface
    val Inset: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.surfaceVariant
    val Ink: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.onSurface
    val SecondaryInk: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.onSurfaceVariant
    val CaptionBlue: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.primary
    val BlueWash: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.primaryContainer
    val LocalTeal: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.secondary
    val TealWash: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.secondaryContainer
    val Border: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.outlineVariant
    val Error: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.error
    val ErrorWash: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.errorContainer
    val Waiting: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.tertiary
}

/** Android-only consumer palette: soft, friendly surfaces with one clear action color. */
private object FriendlyColors {
    object Light {
        val Base = Color(0xFFF5F7F4)
        val Surface = Color(0xFFFFFFFF)
        val Soft = Color(0xFFEDF3EF)
        val High = Color(0xFFE4ECE7)
        val Border = Color(0xFFDDE5E0)
        val Text = Color(0xFF17201C)
        val Secondary = Color(0xFF4D5A54)
        val Muted = Color(0xFF66736D)
        val Primary = Color(0xFF087A68)
        val PrimaryWash = Color(0xFFD8F3EB)
        val Success = Color(0xFF2E7650)
        val SuccessWash = Color(0xFFE2F4E8)
        val Warning = Color(0xFF8A5A00)
        val WarningWash = Color(0xFFFFF0D2)
        val Error = Color(0xFFB3261E)
        val ErrorWash = Color(0xFFFDE8E4)
    }

    object Dark {
        val Base = Color(0xFF0F1513)
        val Surface = Color(0xFF18201D)
        val Soft = Color(0xFF212B27)
        val High = Color(0xFF2B3631)
        val Border = Color(0xFF34413B)
        val Text = Color(0xFFE6EEE9)
        val Secondary = Color(0xFFC1CCC6)
        val Muted = Color(0xFFA2AFA8)
        val Primary = Color(0xFF7ED8C3)
        val OnPrimary = Color(0xFF00382F)
        val PrimaryWash = Color(0xFF184C42)
        val Success = Color(0xFF9AD7AE)
        val SuccessWash = Color(0xFF203F2D)
        val Warning = Color(0xFFF1C66F)
        val WarningWash = Color(0xFF42351C)
        val Error = Color(0xFFFFB4AB)
        val ErrorWash = Color(0xFF4A2522)
    }
}

private val LightReadingScheme = with(FriendlyColors.Light) {
    lightColorScheme(
        primary = Primary, onPrimary = Color.White, primaryContainer = PrimaryWash, onPrimaryContainer = Color(0xFF00382F),
        inversePrimary = FriendlyColors.Dark.Primary,
        secondary = Success, onSecondary = Color.White, secondaryContainer = SuccessWash, onSecondaryContainer = Text,
        tertiary = Warning, onTertiary = Color.White, tertiaryContainer = WarningWash, onTertiaryContainer = Text,
        background = Base, onBackground = Text, surface = Surface, onSurface = Text,
        surfaceVariant = Soft, onSurfaceVariant = Secondary, surfaceTint = Primary,
        inverseSurface = FriendlyColors.Dark.Surface, inverseOnSurface = FriendlyColors.Dark.Text,
        surfaceDim = Base, surfaceBright = Surface, surfaceContainerLowest = Surface, surfaceContainerLow = Base,
        surfaceContainer = Soft, surfaceContainerHigh = Surface, surfaceContainerHighest = High,
        outline = Muted, outlineVariant = Border,
        error = Error, onError = Color.White, errorContainer = ErrorWash, onErrorContainer = Error,
        scrim = Color.Black,
    )
}

private val DarkReadingScheme = with(FriendlyColors.Dark) {
    darkColorScheme(
        primary = Primary, onPrimary = OnPrimary, primaryContainer = PrimaryWash, onPrimaryContainer = Text,
        inversePrimary = FriendlyColors.Light.Primary,
        secondary = Success, onSecondary = OnPrimary, secondaryContainer = SuccessWash, onSecondaryContainer = Text,
        tertiary = Warning, onTertiary = OnPrimary, tertiaryContainer = WarningWash, onTertiaryContainer = Text,
        background = Base, onBackground = Text, surface = Surface, onSurface = Text,
        surfaceVariant = Soft, onSurfaceVariant = Secondary, surfaceTint = Primary,
        inverseSurface = FriendlyColors.Light.Surface, inverseOnSurface = FriendlyColors.Light.Text,
        surfaceDim = Base, surfaceBright = Surface, surfaceContainerLowest = Base, surfaceContainerLow = Base,
        surfaceContainer = Soft, surfaceContainerHigh = Surface, surfaceContainerHighest = High,
        outline = Muted, outlineVariant = Border,
        error = Error, onError = OnPrimary, errorContainer = ErrorWash, onErrorContainer = Error,
        scrim = Color.Black,
    )
}

private val ReadingType = Typography(
    headlineLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Bold, fontSize = 30.sp, lineHeight = 38.sp),
    headlineMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Bold, fontSize = 26.sp, lineHeight = 34.sp),
    headlineSmall = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Bold, fontSize = 23.sp, lineHeight = 31.sp),
    titleLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Bold, fontSize = 20.sp, lineHeight = 28.sp),
    titleMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.SemiBold, fontSize = 17.sp, lineHeight = 24.sp),
    titleSmall = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.SemiBold, fontSize = 15.sp, lineHeight = 22.sp),
    bodyLarge = TextStyle(fontFamily = FontFamily.Default, fontSize = 16.sp, lineHeight = 25.sp),
    bodyMedium = TextStyle(fontFamily = FontFamily.Default, fontSize = 14.sp, lineHeight = 22.sp),
    bodySmall = TextStyle(fontFamily = FontFamily.Default, fontSize = 13.sp, lineHeight = 19.sp),
    labelLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.SemiBold, fontSize = 15.sp, lineHeight = 20.sp),
    labelMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium, fontSize = 13.sp, lineHeight = 18.sp),
)

@Composable
fun FeiniuTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkReadingScheme else LightReadingScheme,
        typography = ReadingType,
        shapes = Shapes(
            extraSmall = RoundedCornerShape(10.dp),
            small = RoundedCornerShape(16.dp),
            medium = RoundedCornerShape(20.dp),
            large = RoundedCornerShape(24.dp),
            extraLarge = RoundedCornerShape(32.dp),
        ),
        content = content,
    )
}

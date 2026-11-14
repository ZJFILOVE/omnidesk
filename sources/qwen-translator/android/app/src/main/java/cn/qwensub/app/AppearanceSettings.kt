package cn.qwensub.app

import android.app.UiModeManager
import android.content.Context
import android.os.Build

internal enum class ThemeMode(val storedValue: String, val label: String) {
    SYSTEM("system", "跟随系统"),
    LIGHT("light", "浅色"),
    DARK("dark", "深色");

    fun isDark(systemDark: Boolean): Boolean = when (this) {
        SYSTEM -> systemDark
        LIGHT -> false
        DARK -> true
    }
}

/** Appearance is independent of connection credentials and never restarts background jobs. */
internal class AppearanceSettings(context: Context) {
    private val appContext = context.applicationContext
    private val preferences = context.getSharedPreferences("appearance", Context.MODE_PRIVATE)

    fun readTheme(): ThemeMode {
        val saved = preferences.getString("theme", null)
        return ThemeMode.entries.firstOrNull { it.storedValue == saved } ?: ThemeMode.SYSTEM
    }

    fun writeTheme(mode: ThemeMode) {
        preferences.edit().putString("theme", mode.storedValue).apply()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            // Also persist the app's night qualifier so the next system splash matches.
            // AUTO clears the app override; it does not change the device's night setting.
            appContext.getSystemService(UiModeManager::class.java).setApplicationNightMode(when (mode) {
                ThemeMode.SYSTEM -> UiModeManager.MODE_NIGHT_AUTO
                ThemeMode.LIGHT -> UiModeManager.MODE_NIGHT_NO
                ThemeMode.DARK -> UiModeManager.MODE_NIGHT_YES
            })
        }
    }
}

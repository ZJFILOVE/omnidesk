package cn.qwensub.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Credentials are encrypted with a non-exportable key and excluded from Android backup. */
internal class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("connection", Context.MODE_PRIVATE)
    private val alias = "qwen-subtitle-connection-v1"

    @Synchronized fun read(): ConnectionSettings = ConnectionSettings(
        baseUrl = prefs.getString("baseUrl", "").orEmpty(),
        token = decrypt(prefs.getString("tokenEncrypted", null)),
        downloadProxy = decrypt(prefs.getString("proxyEncrypted", null)),
    )

    @Synchronized fun write(value: ConnectionSettings) {
        val settings = InputValidation.settings(value)
        check(prefs.edit().putString("baseUrl", settings.baseUrl)
            .putString("tokenEncrypted", encrypt(settings.token))
            .putString("proxyEncrypted", encrypt(settings.downloadProxy)).commit()) { "连接设置保存失败" }
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }

    private fun encrypt(value: String): String {
        if (value.isEmpty()) return ""
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        return Base64.encodeToString(cipher.iv, Base64.NO_WRAP) + ":" +
            Base64.encodeToString(cipher.doFinal(value.toByteArray(Charsets.UTF_8)), Base64.NO_WRAP)
    }

    private fun decrypt(value: String?): String {
        if (value.isNullOrEmpty()) return ""
        return try {
            val parts = value.split(':', limit = 2)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
                init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)))
            }
            String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), Charsets.UTF_8)
        } catch (_: Exception) {
            // A restored/reinstalled app cannot recover an old Keystore key; require token entry.
            ""
        }
    }
}

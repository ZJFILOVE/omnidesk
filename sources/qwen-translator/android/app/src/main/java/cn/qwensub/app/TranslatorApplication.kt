package cn.qwensub.app

import android.app.Application
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class TranslatorApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        // Avoid unpacking Python/FFmpeg or reading task history on the UI thread.
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch { AppRepository.get(this@TranslatorApplication).recover() }
    }
}

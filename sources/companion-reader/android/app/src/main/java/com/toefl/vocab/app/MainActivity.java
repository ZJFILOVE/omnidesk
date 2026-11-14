package com.toefl.vocab.app;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Toast;

import com.getcapacitor.BridgeActivity;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

public class MainActivity extends BridgeActivity {
    private static final String MODEL = "gpt-6-luna";
    private static final String KEY_ALIAS = "toefl.companion.feiniu";
    private TextToSpeech tts;
    private volatile boolean ttsReady = false;
    private volatile boolean nativeSpeaking = false;
    private volatile String lastUtteranceId = "";
    private volatile List<String> lastRequestedChunks = new ArrayList<>();
    private volatile String lastRequestedAccent = "us";
    private MediaPlayer mediaPlayer;
    private final AtomicInteger speakGeneration = new AtomicInteger(0);
    private volatile String pendingIncomingJson = "";

    @Override
    public void onCreate(Bundle savedInstanceState) {
        importPrivateProvisionIfPresent();
        initTts();
        captureIncomingIntent(getIntent());
        super.onCreate(savedInstanceState);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (captureIncomingIntent(intent)) {
            notifyJsIncomingText();
        }
    }

    private boolean captureIncomingIntent(Intent intent) {
        if (intent == null || intent.getAction() == null) return false;
        String action = intent.getAction();
        CharSequence rawText = null;
        String sourceKind = "";
        if (Intent.ACTION_PROCESS_TEXT.equals(action)) {
            rawText = intent.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT);
            sourceKind = "来自系统选词";
        } else if (Intent.ACTION_SEND.equals(action)) {
            rawText = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
            sourceKind = "来自系统分享";
        }
        if (rawText == null || rawText.toString().trim().isEmpty()) return false;
        try {
            String clean = rawText.toString().trim();
            if (clean.length() > 12000) clean = clean.substring(0, 12000);
            pendingIncomingJson = new JSONObject()
                    .put("text", clean)
                    .put("source", sourceKind)
                    .toString();
            return true;
        } catch (Exception ignored) {
            return false;
        }
    }

    private void notifyJsIncomingText() {
        final String payload = pendingIncomingJson;
        if (payload == null || payload.isEmpty()) return;
        runOnUiThread(() -> {
            try {
                WebView wv = getBridge() != null ? getBridge().getWebView() : findViewById(com.getcapacitor.android.R.id.webview);
                if (wv != null) {
                    pendingIncomingJson = "";
                    String escaped = JSONObject.quote(payload);
                    wv.evaluateJavascript("window.__onNativeIncomingText && window.__onNativeIncomingText(" + escaped + ")", null);
                }
            } catch (Exception ignored) {
            }
        });
    }

    @Override
    protected void load() {
        WebView preWebView = findViewById(com.getcapacitor.android.R.id.webview);
        if (preWebView != null) {
            WebSettings settings = preWebView.getSettings();
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
            settings.setMediaPlaybackRequiresUserGesture(false);
            preWebView.addJavascriptInterface(new CompanionBridge(), "AndroidBridge");
        }
        super.load();
        if (getBridge() != null && getBridge().getWebView() != null && preWebView == null) {
            WebView webView = getBridge().getWebView();
            WebSettings settings = webView.getSettings();
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
            settings.setMediaPlaybackRequiresUserGesture(false);
            webView.addJavascriptInterface(new CompanionBridge(), "AndroidBridge");
        }
    }

    private void initTts() {
        try {
            tts = new TextToSpeech(getApplicationContext(), status -> {
                if (status == TextToSpeech.SUCCESS && tts != null) {
                    configureTtsEngine(tts);
                } else {
                    initExplicitOplusTts();
                }
            });
        } catch (Exception ignored) {
            initExplicitOplusTts();
        }
    }

    private void initExplicitOplusTts() {
        try {
            tts = new TextToSpeech(getApplicationContext(), status -> {
                if (status == TextToSpeech.SUCCESS && tts != null) {
                    configureTtsEngine(tts);
                } else {
                    ttsReady = false;
                }
            }, "com.oplus.ttsaccessibilityengine");
        } catch (Exception ignored) {
            ttsReady = false;
        }
    }

    private void configureTtsEngine(TextToSpeech engine) {
        try {
            engine.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            int res = engine.setLanguage(Locale.US);
            if (res == TextToSpeech.LANG_MISSING_DATA || res == TextToSpeech.LANG_NOT_SUPPORTED) {
                engine.setLanguage(Locale.SIMPLIFIED_CHINESE);
            }
            engine.setSpeechRate(0.95f);
            engine.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override
                public void onStart(String utteranceId) {
                    nativeSpeaking = true;
                }

                @Override
                public void onDone(String utteranceId) {
                    if (utteranceId != null && utteranceId.equals(lastUtteranceId)) {
                        nativeSpeaking = false;
                        notifyJsAudioEnded();
                    }
                }

                @Override
                public void onError(String utteranceId) {
                    List<String> fallbackChunks = lastRequestedChunks;
                    if (fallbackChunks != null && !fallbackChunks.isEmpty()) {
                        lastRequestedChunks = new ArrayList<>();
                        speakViaMediaPlayerAsync(fallbackChunks, lastRequestedAccent, speakGeneration.get());
                    } else {
                        nativeSpeaking = false;
                        notifyJsAudioEnded();
                    }
                }
            });
            ttsReady = true;
        } catch (Exception ignored) {
            ttsReady = false;
        }
    }

    private void notifyJsAudioEnded() {
        runOnUiThread(() -> {
            try {
                WebView wv = getBridge() != null ? getBridge().getWebView() : findViewById(com.getcapacitor.android.R.id.webview);
                if (wv != null) {
                    wv.evaluateJavascript("window.__onNativeAudioEnded && window.__onNativeAudioEnded()", null);
                }
            } catch (Exception ignored) {
            }
        });
    }

    private synchronized void stopMediaPlayerInternal() {
        if (mediaPlayer != null) {
            try {
                if (mediaPlayer.isPlaying()) {
                    mediaPlayer.stop();
                }
            } catch (Exception ignored) {
            }
            try {
                mediaPlayer.release();
            } catch (Exception ignored) {
            }
            mediaPlayer = null;
        }
    }

    private static List<String> splitSpeechChunks(String raw) {
        List<String> chunks = new ArrayList<>();
        String cleaned = raw == null ? "" : raw.replaceAll("\\s+", " ").trim();
        if (cleaned.isEmpty()) return chunks;
        if (cleaned.length() <= 170) {
            chunks.add(cleaned);
            return chunks;
        }
        String[] sentences = cleaned.split("(?<=[.!?;。！？；])\\s+");
        StringBuilder current = new StringBuilder();
        for (String sentence : sentences) {
            String s = sentence.trim();
            if (s.isEmpty()) continue;
            if (current.length() + s.length() + 1 <= 170) {
                if (current.length() > 0) current.append(" ");
                current.append(s);
            } else {
                if (current.length() > 0) {
                    chunks.add(current.toString());
                    current.setLength(0);
                }
                if (s.length() <= 170) {
                    current.append(s);
                } else {
                    String[] words = s.split("\\s+");
                    for (String w : words) {
                        if (current.length() + w.length() + 1 <= 170) {
                            if (current.length() > 0) current.append(" ");
                            current.append(w);
                        } else {
                            if (current.length() > 0) chunks.add(current.toString());
                            current.setLength(0);
                            current.append(w);
                        }
                    }
                }
            }
        }
        if (current.length() > 0) {
            chunks.add(current.toString());
        }
        return chunks;
    }

    private File fetchOrCacheChunkMp3(String chunk, String accent) {
        try {
            File cacheDir = new File(getCacheDir(), "tts_audio_cache");
            if (!cacheDir.exists()) {
                //noinspection ResultOfMethodCallIgnored
                cacheDir.mkdirs();
            }
            String normAccent = "uk".equalsIgnoreCase(accent) ? "uk" : "us";
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest((normAccent + "::" + chunk).getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (int i = 0; i < 14 && i < digest.length; i++) {
                hex.append(String.format(Locale.US, "%02x", digest[i]));
            }
            File target = new File(cacheDir, hex + ".mp3");
            if (target.isFile() && target.length() > 400) {
                return target;
            }
            List<String> urls = new ArrayList<>();
            String encoded = URLEncoder.encode(chunk, "UTF-8");
            if (!chunk.contains(" ") && chunk.length() <= 32) {
                String ydType = "uk".equals(normAccent) ? "1" : "2";
                urls.add("https://dict.youdao.com/dictvoice?audio=" + encoded + "&type=" + ydType);
            }
            String gLang = "uk".equals(normAccent) ? "en-GB" : "en-US";
            urls.add("https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=" + gLang + "&q=" + encoded);
            String bdLan = "uk".equals(normAccent) ? "uk" : "en";
            urls.add("https://fanyi.baidu.com/gettts?lan=" + bdLan + "&text=" + encoded + "&spd=5&source=web");

            for (String u : urls) {
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(u).openConnection();
                    conn.setConnectTimeout(5000);
                    conn.setReadTimeout(8000);
                    conn.setRequestProperty("User-Agent", "Mozilla/5.0");
                    if (conn.getResponseCode() == 200) {
                        ByteArrayOutputStream bos = new ByteArrayOutputStream();
                        try (InputStream in = conn.getInputStream()) {
                            byte[] buf = new byte[4096];
                            int n;
                            while ((n = in.read(buf)) != -1) {
                                bos.write(buf, 0, n);
                                if (bos.size() > 2 * 1024 * 1024) break;
                            }
                        }
                        byte[] audioBytes = bos.toByteArray();
                        if (audioBytes.length > 400 && audioBytes[0] != '{' && audioBytes[0] != '<') {
                            try (FileOutputStream fos = new FileOutputStream(target)) {
                                fos.write(audioBytes);
                            }
                            return target;
                        }
                    }
                } catch (Exception ignored) {
                } finally {
                    if (conn != null) conn.disconnect();
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    private void speakViaMediaPlayerAsync(List<String> chunks, String accent, int gen) {
        new Thread(() -> {
            try {
                for (String chunk : chunks) {
                    if (speakGeneration.get() != gen) return;
                    File mp3 = fetchOrCacheChunkMp3(chunk, accent);
                    if (mp3 == null || speakGeneration.get() != gen) {
                        break;
                    }
                    final Object waitLock = new Object();
                    final boolean[] finished = {false};
                    synchronized (this) {
                        stopMediaPlayerInternal();
                        mediaPlayer = new MediaPlayer();
                        mediaPlayer.setAudioAttributes(new AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_MEDIA)
                                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                                .build());
                        try (FileInputStream fis = new FileInputStream(mp3)) {
                            mediaPlayer.setDataSource(fis.getFD());
                            mediaPlayer.prepare();
                        }
                        mediaPlayer.setOnCompletionListener(mp -> {
                            synchronized (waitLock) {
                                finished[0] = true;
                                waitLock.notifyAll();
                            }
                        });
                        mediaPlayer.setOnErrorListener((mp, what, extra) -> {
                            synchronized (waitLock) {
                                finished[0] = true;
                                waitLock.notifyAll();
                            }
                            return true;
                        });
                        mediaPlayer.start();
                    }
                    synchronized (waitLock) {
                        while (!finished[0] && speakGeneration.get() == gen) {
                            waitLock.wait(250);
                        }
                    }
                }
            } catch (Exception ignored) {
            } finally {
                if (speakGeneration.get() == gen) {
                    stopMediaPlayerInternal();
                    nativeSpeaking = false;
                    notifyJsAudioEnded();
                }
            }
        }).start();
    }

    @Override
    public void onDestroy() {
        speakGeneration.incrementAndGet();
        stopMediaPlayerInternal();
        if (tts != null) {
            try {
                tts.stop();
                tts.shutdown();
            } catch (Exception ignored) {
            }
        }
        super.onDestroy();
    }

    private SharedPreferences prefs() {
        return getSharedPreferences("feiniu_connection", Context.MODE_PRIVATE);
    }

    private SecretKey secretKey() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        KeyStore.Entry existing = store.getEntry(KEY_ALIAS, null);
        if (existing instanceof KeyStore.SecretKeyEntry) {
            return ((KeyStore.SecretKeyEntry) existing).getSecretKey();
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
        )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build());
        return generator.generateKey();
    }

    private String getBaseUrl() {
        return prefs().getString("base_url", "http://100.90.158.48:8766/v1");
    }

    private boolean isConfigured() {
        return prefs().getBoolean("configured", false) && !getApiKey().isEmpty();
    }

    private String getApiKey() {
        try {
            String raw = prefs().getString("secret", null);
            if (raw == null || raw.isEmpty()) return "";
            byte[] data = Base64.decode(raw, Base64.NO_WRAP);
            if (data.length <= 12) return "";
            byte[] iv = Arrays.copyOfRange(data, 0, 12);
            byte[] cipherBytes = Arrays.copyOfRange(data, 12, data.length);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, secretKey(), new GCMParameterSpec(128, iv));
            return new String(cipher.doFinal(cipherBytes), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        }
    }

    private void saveConnectionInternal(String rawUrl, String secret) throws Exception {
        String cleanUrl = normalizeBaseUrl(rawUrl);
        String cleanSecret = secret == null ? "" : secret.trim();
        if (cleanSecret.contains("\n") || cleanSecret.contains("\r")) {
            throw new IllegalArgumentException("令牌不能包含换行");
        }
        SharedPreferences.Editor editor = prefs().edit().putString("base_url", cleanUrl);
        if (!cleanSecret.isEmpty()) {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, secretKey());
            byte[] iv = cipher.getIV();
            byte[] encrypted = cipher.doFinal(cleanSecret.getBytes(StandardCharsets.UTF_8));
            byte[] combined = new byte[iv.length + encrypted.length];
            System.arraycopy(iv, 0, combined, 0, iv.length);
            System.arraycopy(encrypted, 0, combined, iv.length, encrypted.length);
            editor.putString("secret", Base64.encodeToString(combined, Base64.NO_WRAP));
            editor.putBoolean("configured", true);
        } else if (!getApiKey().isEmpty()) {
            editor.putBoolean("configured", true);
        }
        if (!editor.commit()) {
            throw new IllegalStateException("保存连接设置失败");
        }
    }

    private String normalizeBaseUrl(String raw) {
        if (raw == null || raw.trim().isEmpty()) {
            throw new IllegalArgumentException("请输入飞牛服务地址");
        }
        String trimmed = raw.trim().replaceAll("/+$", "");
        URI uri = URI.create(trimmed);
        String scheme = uri.getScheme();
        String host = uri.getHost();
        if (scheme == null || host == null || (!"http".equalsIgnoreCase(scheme) && !"https".equalsIgnoreCase(scheme))) {
            throw new IllegalArgumentException("请输入有效的 HTTP/HTTPS 地址");
        }
        if ("http".equalsIgnoreCase(scheme) && !isPrivateHost(host)) {
            throw new IllegalArgumentException("公网地址请使用 HTTPS；HTTP 仅限飞牛 Tailscale 或局域网地址");
        }
        if (!trimmed.endsWith("/v1")) {
            trimmed = trimmed + "/v1";
        }
        return trimmed;
    }

    private boolean isPrivateHost(String host) {
        if ("localhost".equalsIgnoreCase(host) || "::1".equals(host)) return true;
        String[] parts = host.split("\\.");
        if (parts.length != 4) return false;
        int[] nums = new int[4];
        for (int i = 0; i < 4; i++) {
            try {
                nums[i] = Integer.parseInt(parts[i]);
            } catch (NumberFormatException e) {
                return false;
            }
            if (nums[i] < 0 || nums[i] > 255) return false;
        }
        return nums[0] == 127
                || nums[0] == 10
                || (nums[0] == 192 && nums[1] == 168)
                || (nums[0] == 172 && nums[1] >= 16 && nums[1] <= 31)
                || (nums[0] == 100 && nums[1] >= 64 && nums[1] <= 127);
    }

    private void importPrivateProvisionIfPresent() {
        File file = new File(getNoBackupFilesDir(), "provision.json");
        if (!file.isFile()) return;
        try {
            if (file.length() <= 16384) {
                byte[] bytes = new byte[(int) file.length()];
                try (FileInputStream in = new FileInputStream(file)) {
                    int read = in.read(bytes);
                    if (read > 0) {
                        JSONObject obj = new JSONObject(new String(bytes, 0, read, StandardCharsets.UTF_8));
                        saveConnectionInternal(
                                obj.optString("base_url", "http://100.90.158.48:8766/v1"),
                                obj.optString("api_key", "")
                        );
                    }
                }
            }
        } catch (Exception ignored) {
        } finally {
            // Delete plaintext provisioning file immediately after importing to Keystore
            //noinspection ResultOfMethodCallIgnored
            file.delete();
        }
    }

    private static String readStream(InputStream stream) throws Exception {
        if (stream == null) return "";
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        int total = 0;
        while ((n = stream.read(buf)) != -1) {
            total += n;
            if (total > 1024 * 1024) {
                throw new IllegalStateException("响应内容过大");
            }
            out.write(buf, 0, n);
        }
        return new String(out.toByteArray(), StandardCharsets.UTF_8);
    }

    public class CompanionBridge {
        @JavascriptInterface
        public String getConnectionConfig() {
            try {
                importPrivateProvisionIfPresent();
                JSONObject res = new JSONObject();
                res.put("baseUrl", getBaseUrl());
                res.put("configured", isConfigured());
                res.put("model", MODEL);
                res.put("ttsReady", ttsReady);
                return res.toString();
            } catch (Exception e) {
                return "{\"baseUrl\":\"http://100.90.158.48:8766/v1\",\"configured\":false,\"model\":\"gpt-6-luna\"}";
            }
        }

        @JavascriptInterface
        public String saveConnectionConfig(String baseUrl, String apiKey) {
            try {
                saveConnectionInternal(baseUrl, apiKey);
                JSONObject res = new JSONObject();
                res.put("ok", true);
                res.put("baseUrl", getBaseUrl());
                res.put("configured", isConfigured());
                return res.toString();
            } catch (Exception e) {
                try {
                    return new JSONObject().put("ok", false).put("error", e.getMessage()).toString();
                } catch (Exception ignored) {
                    return "{\"ok\":false,\"error\":\"保存失败\"}";
                }
            }
        }

        @JavascriptInterface
        public String testConnection() {
            HttpURLConnection conn = null;
            try {
                importPrivateProvisionIfPresent();
                String base = getBaseUrl();
                String key = getApiKey();
                if (key.isEmpty()) {
                    return new JSONObject().put("ok", false).put("error", "尚未填写飞牛访问令牌").toString();
                }
                String healthUrl = base.endsWith("/v1") ? base + "/health" : base + "/v1/health";
                conn = (HttpURLConnection) new URL(healthUrl).openConnection();
                conn.setInstanceFollowRedirects(false);
                conn.setConnectTimeout(8000);
                conn.setReadTimeout(10000);
                conn.setRequestMethod("GET");
                conn.setRequestProperty("Authorization", "Bearer " + key);
                int code = conn.getResponseCode();
                String body = readStream(code >= 200 && code < 300 ? conn.getInputStream() : conn.getErrorStream());
                if (code == 404) {
                    // Fallback to root /health if /v1/health not mounted
                    conn.disconnect();
                    String rootHealth = base.replaceFirst("/v1$", "") + "/health";
                    conn = (HttpURLConnection) new URL(rootHealth).openConnection();
                    conn.setInstanceFollowRedirects(false);
                    conn.setConnectTimeout(8000);
                    conn.setReadTimeout(10000);
                    conn.setRequestMethod("GET");
                    conn.setRequestProperty("Authorization", "Bearer " + key);
                    code = conn.getResponseCode();
                    body = readStream(code >= 200 && code < 300 ? conn.getInputStream() : conn.getErrorStream());
                }
                if (code >= 200 && code < 300) {
                    JSONObject data = new JSONObject(body);
                    return new JSONObject()
                            .put("ok", true)
                            .put("model", data.optString("model", MODEL))
                            .put("port", data.optInt("port", 8766))
                            .toString();
                }
                return new JSONObject()
                        .put("ok", false)
                        .put("error", code == 401 || code == 403 ? "飞牛令牌无效，请检查设置" : "连接返回 HTTP " + code)
                        .toString();
            } catch (Exception e) {
                try {
                    return new JSONObject()
                            .put("ok", false)
                            .put("error", "无法连接飞牛 8766 服务，请确认 Tailscale 已开启")
                            .toString();
                } catch (Exception ignored) {
                    return "{\"ok\":false,\"error\":\"连接失败\"}";
                }
            } finally {
                if (conn != null) conn.disconnect();
            }
        }

        @JavascriptInterface
        public String chatCompletion(String systemPrompt, String userPrompt) {
            HttpURLConnection conn = null;
            try {
                importPrivateProvisionIfPresent();
                String base = getBaseUrl();
                String key = getApiKey();
                if (key.isEmpty()) {
                    return new JSONObject().put("ok", false).put("error", "请先在右上角设置中配置飞牛 8766 访问令牌").toString();
                }
                String endpoint = base.endsWith("/chat/completions") ? base : base + "/chat/completions";
                JSONObject payload = new JSONObject()
                        .put("model", MODEL)
                        .put("reasoning_effort", "low")
                        .put("stream", false)
                        .put("messages", new JSONArray()
                                .put(new JSONObject().put("role", "system").put("content", systemPrompt))
                                .put(new JSONObject().put("role", "user").put("content", userPrompt)));

                byte[] bodyBytes = payload.toString().getBytes(StandardCharsets.UTF_8);
                conn = (HttpURLConnection) new URL(endpoint).openConnection();
                conn.setInstanceFollowRedirects(false);
                conn.setConnectTimeout(15000);
                conn.setReadTimeout(110000);
                conn.setRequestMethod("POST");
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                conn.setRequestProperty("Authorization", "Bearer " + key);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(bodyBytes);
                }
                int code = conn.getResponseCode();
                String respText = readStream(code >= 200 && code < 300 ? conn.getInputStream() : conn.getErrorStream());
                if (code < 200 || code >= 300) {
                    String msg = code == 401 || code == 403
                            ? "飞牛令牌无效或无权限，请检查连接设置"
                            : code == 429
                            ? "飞牛 6-luna 正在处理其他请求，请稍后重试"
                            : "请求飞牛 6-luna 失败 (HTTP " + code + ")";
                    return new JSONObject().put("ok", false).put("error", msg).toString();
                }
                JSONObject root = new JSONObject(respText);
                JSONObject choice = root.optJSONArray("choices") != null
                        ? root.optJSONArray("choices").optJSONObject(0)
                        : null;
                String content = choice != null && choice.optJSONObject("message") != null
                        ? choice.optJSONObject("message").optString("content", "").trim()
                        : "";
                if (content.isEmpty()) {
                    return new JSONObject().put("ok", false).put("error", "飞牛 6-luna 返回了空内容，请重试").toString();
                }
                return new JSONObject()
                        .put("ok", true)
                        .put("content", content)
                        .put("model", root.optString("model", MODEL))
                        .toString();
            } catch (Exception e) {
                try {
                    return new JSONObject()
                            .put("ok", false)
                            .put("error", "连接飞牛 6-luna 超时或断开，请检查 Tailscale 与网络")
                            .toString();
                } catch (Exception ignored) {
                    return "{\"ok\":false,\"error\":\"请求失败\"}";
                }
            } finally {
                if (conn != null) conn.disconnect();
            }
        }

        @JavascriptInterface
        public String searchSubtitles(String query, int limit) {
            HttpURLConnection conn = null;
            try {
                importPrivateProvisionIfPresent();
                String clean = query == null ? "" : query.trim();
                if (clean.isEmpty()) {
                    return new JSONObject().put("ok", false).put("error", "请输入要检索的单词").toString();
                }
                String base = getBaseUrl();
                String key = getApiKey();
                if (!key.isEmpty()) {
                    try {
                        String url = base + "/subtitles/search?q="
                                + URLEncoder.encode(clean, "UTF-8")
                                + "&limit=" + Math.max(1, Math.min(limit, 20));
                        conn = (HttpURLConnection) new URL(url).openConnection();
                        conn.setInstanceFollowRedirects(false);
                        conn.setConnectTimeout(10000);
                        conn.setReadTimeout(20000);
                        conn.setRequestMethod("GET");
                        conn.setRequestProperty("Authorization", "Bearer " + key);
                        int code = conn.getResponseCode();
                        if (code >= 200 && code < 300) {
                            String body = readStream(conn.getInputStream());
                            JSONObject data = new JSONObject(body);
                            return new JSONObject()
                                    .put("ok", true)
                                    .put("matches", data.optJSONArray("matches") != null ? data.optJSONArray("matches") : new JSONArray())
                                    .put("via", "feiniu-8766")
                                    .toString();
                        }
                    } catch (Exception ignored) {
                    } finally {
                        if (conn != null) {
                            conn.disconnect();
                            conn = null;
                        }
                    }
                }
                // Direct fallback to QuoDB quote search if Feiniu is offline
                String encoded = URLEncoder.encode(clean.toLowerCase(Locale.US), "UTF-8");
                String directUrl = "http://api.quodb.com/search/" + encoded
                        + "?advance-search=false&keywords=" + encoded
                        + "&titles_per_page=" + Math.max(1, Math.min(limit, 10))
                        + "&phrases_per_title=1&page=1";
                conn = (HttpURLConnection) new URL(directUrl).openConnection();
                conn.setConnectTimeout(10000);
                conn.setReadTimeout(12000);
                conn.setRequestProperty("User-Agent", "Mozilla/5.0");
                conn.setRequestProperty("Accept", "application/json");
                int code = conn.getResponseCode();
                if (code >= 200 && code < 300) {
                    JSONObject data = new JSONObject(readStream(conn.getInputStream()));
                    JSONArray docs = data.optJSONArray("docs");
                    JSONArray matches = new JSONArray();
                    if (docs != null) {
                        for (int i = 0; i < Math.min(docs.length(), limit); i++) {
                            JSONObject d = docs.optJSONObject(i);
                            if (d == null) continue;
                            String phrase = d.optString("phrase", "").trim();
                            String titleId = d.optString("title_id", "");
                            long phraseId = d.optLong("phrase_id", 0);
                            if (phrase.isEmpty()) continue;
                            JSONArray prev = new JSONArray();
                            JSONArray next = new JSONArray();
                            if (!titleId.isEmpty() && phraseId > 0) {
                                HttpURLConnection ctxConn = null;
                                try {
                                    ctxConn = (HttpURLConnection) new URL("http://api.quodb.com/quotes/" + titleId + "/" + phraseId).openConnection();
                                    ctxConn.setConnectTimeout(5000);
                                    ctxConn.setReadTimeout(6000);
                                    ctxConn.setRequestProperty("User-Agent", "Mozilla/5.0");
                                    if (ctxConn.getResponseCode() == 200) {
                                        JSONArray ctxDocs = new JSONObject(readStream(ctxConn.getInputStream())).optJSONArray("docs");
                                        if (ctxDocs != null) {
                                            int pos = -1;
                                            for (int j = 0; j < ctxDocs.length(); j++) {
                                                if (ctxDocs.optJSONObject(j) != null && ctxDocs.optJSONObject(j).optLong("phrase_id") == phraseId) {
                                                    pos = j;
                                                    break;
                                                }
                                            }
                                            if (pos >= 0) {
                                                for (int j = Math.max(0, pos - 2); j < pos; j++) {
                                                    prev.put(ctxDocs.optJSONObject(j).optString("phrase", "").trim());
                                                }
                                                for (int j = pos + 1; j < Math.min(ctxDocs.length(), pos + 3); j++) {
                                                    next.put(ctxDocs.optJSONObject(j).optString("phrase", "").trim());
                                                }
                                            }
                                        }
                                    }
                                } catch (Exception ignored) {
                                } finally {
                                    if (ctxConn != null) ctxConn.disconnect();
                                }
                            }
                            double startSec = Math.round((d.optDouble("time", 0) / 1000.0) * 100.0) / 100.0;
                            String lowerPhrase = phrase.toLowerCase(Locale.US);
                            String lowerClean = clean.toLowerCase(Locale.US);
                            int mStart = Math.max(0, lowerPhrase.indexOf(lowerClean));
                            int mEnd = Math.min(phrase.length(), mStart + clean.length());
                            String showTitle = !d.optString("serie", "").isEmpty() && !d.isNull("serie")
                                    ? d.optString("serie")
                                    : d.optString("title", "Unknown Title");
                            matches.put(new JSONObject()
                                    .put("id", "quodb:" + titleId + ":" + phraseId)
                                    .put("source", "online")
                                    .put("show", showTitle)
                                    .put("episode", d.optInt("year", 0) > 0 ? String.valueOf(d.optInt("year")) : JSONObject.NULL)
                                    .put("year", d.optInt("year", 0))
                                    .put("start", startSec)
                                    .put("end", startSec + 3.5)
                                    .put("text", phrase)
                                    .put("matchedForm", phrase.substring(mStart, mEnd))
                                    .put("matchStart", mStart)
                                    .put("matchEnd", mEnd)
                                    .put("previous", prev)
                                    .put("next", next)
                                    .put("variant", false));
                        }
                    }
                    return new JSONObject().put("ok", true).put("matches", matches).put("via", "direct").toString();
                }
                return new JSONObject().put("ok", false).put("error", "字幕检索服务暂时不可用").toString();
            } catch (Exception e) {
                try {
                    return new JSONObject().put("ok", false).put("error", "检索台词失败，请检查网络连接").toString();
                } catch (Exception ignored) {
                    return "{\"ok\":false,\"error\":\"检索失败\"}";
                }
            } finally {
                if (conn != null) conn.disconnect();
            }
        }

        @JavascriptInterface
        public boolean speak(String text, String accent) {
            if (text == null || text.trim().isEmpty()) return false;
            int gen = speakGeneration.incrementAndGet();
            stopMediaPlayerInternal();
            if (tts != null) {
                try {
                    tts.stop();
                } catch (Exception ignored) {
                }
            }
            List<String> chunks = splitSpeechChunks(text);
            if (chunks.isEmpty()) return false;
            nativeSpeaking = true;
            lastRequestedChunks = chunks;
            lastRequestedAccent = accent == null ? "us" : accent;

            if (tts != null && ttsReady) {
                try {
                    Locale target = "uk".equalsIgnoreCase(accent) ? Locale.UK : Locale.US;
                    int langRes = tts.setLanguage(target);
                    if (langRes == TextToSpeech.LANG_MISSING_DATA || langRes == TextToSpeech.LANG_NOT_SUPPORTED) {
                        int usRes = tts.setLanguage(Locale.US);
                        if (usRes == TextToSpeech.LANG_MISSING_DATA || usRes == TextToSpeech.LANG_NOT_SUPPORTED) {
                            tts.setLanguage(Locale.SIMPLIFIED_CHINESE);
                        }
                    }
                    tts.setSpeechRate(0.94f);
                    boolean allQueued = true;
                    for (int i = 0; i < chunks.size(); i++) {
                        String uid = "utt-" + gen + "-" + i + "-" + UUID.randomUUID();
                        if (i == chunks.size() - 1) {
                            lastUtteranceId = uid;
                        }
                        int mode = i == 0 ? TextToSpeech.QUEUE_FLUSH : TextToSpeech.QUEUE_ADD;
                        int res = tts.speak(chunks.get(i), mode, null, uid);
                        if (res != TextToSpeech.SUCCESS) {
                            allQueued = false;
                            break;
                        }
                    }
                    if (allQueued) {
                        return true;
                    }
                } catch (Exception ignored) {
                }
            }
            try {
                speakViaMediaPlayerAsync(chunks, accent, gen);
                return true;
            } catch (Exception e) {
                nativeSpeaking = false;
                return false;
            }
        }

        @JavascriptInterface
        public void stopSpeaking() {
            speakGeneration.incrementAndGet();
            nativeSpeaking = false;
            stopMediaPlayerInternal();
            if (tts != null) {
                try {
                    tts.stop();
                } catch (Exception ignored) {
                }
            }
        }

        @JavascriptInterface
        public boolean isSpeaking() {
            if (nativeSpeaking) {
                try {
                    if (tts != null && ttsReady && tts.isSpeaking()) return true;
                } catch (Exception ignored) {
                }
                synchronized (MainActivity.this) {
                    if (mediaPlayer != null) {
                        try {
                            if (mediaPlayer.isPlaying()) return true;
                        } catch (Exception ignored) {
                        }
                    }
                }
            }
            return nativeSpeaking;
        }

        @JavascriptInterface
        public String consumeIncomingText() {
            String out = pendingIncomingJson;
            pendingIncomingJson = "";
            return out == null ? "" : out;
        }

        @JavascriptInterface
        public boolean copyToClipboard(String text) {
            if (text == null || text.trim().isEmpty()) return false;
            runOnUiThread(() -> {
                try {
                    ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                    if (cm != null) {
                        cm.setPrimaryClip(ClipData.newPlainText("伴读结果", text));
                        Toast.makeText(MainActivity.this, "已复制到剪贴板", Toast.LENGTH_SHORT).show();
                    }
                } catch (Exception ignored) {
                }
            });
            return true;
        }
    }
}

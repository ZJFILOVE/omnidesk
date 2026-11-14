const element = (name) => document.getElementById(name);
let socket;
const preferenceFields = ["region", "mode", "headphones", "microphone", "inbound_voice", "outbound_voice"];
let preferences = {};
try { preferences = JSON.parse(localStorage.getItem("qwen_duplex_preferences") || "{}"); } catch {}
if (!preferences || typeof preferences !== "object") preferences = {};

function savePreferences() {
  preferences = Object.fromEntries(preferenceFields.map(name => [name, element(name).value]));
  try { localStorage.setItem("qwen_duplex_preferences", JSON.stringify(preferences)); } catch {}
}

function showRoute() {
  const mode = element("mode").value;
  const headphones = element("headphones").selectedOptions[0]?.textContent || "未选择耳机/扬声器";
  const microphone = element("microphone").selectedOptions[0]?.textContent || "未选择麦克风";
  const routes = [];
  if (["inbound", "duplex"].includes(mode)) routes.push(`对方外语 → 中文语音 → ${headphones}`);
  if (["outbound", "duplex", "mic_test"].includes(mode)) routes.push(`${microphone} → 英文语音 → ${mode === "mic_test" ? headphones : "会议虚拟麦克风（耳机不播放）"}`);
  element("route").textContent = "声音走向：" + routes.join("  ｜  ");
}

function choosePreferredDevice(name, items, devices) {
  if (!items.length) return null;
  if (name === "headphones") {
    return (
      items.find(item => item.name === devices.default_sink) ||
      items.find(item => /bluez|bluetooth|headphone|headset|earphone|耳机|usb/i.test(`${item.name} ${item.description}`)) ||
      items.find(item => !item.mute) ||
      items[0]
    );
  }
  const unmuted = items.filter(item => !item.mute);
  const pool = unmuted.length ? unmuted : items;
  return (
    pool.find(item => item.name === devices.default_source) ||
    pool.find(item => /bluez|bluetooth|headset|mic|usb|麦克风|耳麦/i.test(`${item.name} ${item.description}`)) ||
    pool[0]
  );
}

for (const name of preferenceFields) element(name).addEventListener("change", () => { savePreferences(); showRoute(); });

async function refresh(setup = false) {
  try {
    const response = await fetch(setup ? "/api/setup" : "/api/devices", {method: setup ? "POST" : "GET"});
    if (!response.ok) throw new Error(await response.text());
    const devices = await response.json();
    for (const [name, items] of [["headphones", devices.sinks.filter(item => !["meeting-remote", "meeting-translator"].includes(item.name))], ["microphone", devices.sources.filter(item => !item.name.endsWith(".monitor"))]]) {
      const select = element(name);
      const previous = select.value || preferences[name];
      select.replaceChildren(...items.map(item => new Option(`${item.description}${item.mute ? "（静音）" : ""}`, item.name)));
      if (items.some(item => item.name === previous)) select.value = previous;
      else {
        const preferred = choosePreferredDevice(name, items, devices);
        if (preferred) select.value = preferred.name;
      }
    }
    element("error").textContent = "";
    element("status").textContent = devices.sinks.some(item => item.name === "meeting-remote") ? "设备已就绪，填写密钥后即可开始" : "如需连接线上会议，可在高级设置中初始化虚拟音频";
    showRoute();
  } catch (error) { element("error").textContent = error.message; }
}

element("refresh").onclick = () => refresh();
element("setup").onclick = () => refresh(true);
element("start").onclick = () => {
  if (!element("key").value.trim()) { element("error").textContent = "请先输入百炼 API Key"; return; }
  element("error").textContent = "";
  element("inbound").textContent = "";
  element("outbound").textContent = "";
  element("settings").disabled = true;
  element("start").disabled = true;
  element("stop").disabled = false;
  element("status").textContent = "正在连接百炼…";
  savePreferences();
  showRoute();
  socket = new WebSocket(`ws://${location.host}/ws/duplex`);
  socket.onopen = () => socket.send(JSON.stringify({api_key: element("key").value.trim(), region: element("region").value, mode: element("mode").value, headphones: element("headphones").value, microphone: element("microphone").value, inbound_voice: element("inbound_voice").value, outbound_voice: element("outbound_voice").value}));
  socket.onmessage = ({data}) => {
    const event = JSON.parse(data);
    if (event.error) element("error").textContent = event.error;
    if (event.status) element("status").textContent = `${event.direction === "inbound" ? "对方 → 你" : element("mode").value === "mic_test" ? "你 → 耳机英文试听" : "你 → 对方"}：${event.status}`;
    const caption = event.source_final ? `原文：${event.source_final}` : event.translation_final ? `译文：${event.translation_final}` : "";
    if (caption && event.direction) {
      const panel = element(event.direction);
      panel.textContent = (panel.textContent + caption + "\n\n").slice(-20000);
      panel.scrollTop = panel.scrollHeight;
    }
  };
  socket.onerror = () => { element("error").textContent = "本地连接异常，请检查服务是否运行"; };
  socket.onclose = () => {
    element("settings").disabled = false;
    element("start").disabled = false;
    element("stop").disabled = true;
    element("status").textContent = "已停止";
    socket = null;
  };
};
element("stop").onclick = () => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send("stop");
    element("status").textContent = "正在停止…";
    element("stop").disabled = true;
  } else socket?.close();
};
window.addEventListener("pagehide", () => socket?.close());
async function initialize() {
  element("start").disabled = true;
  try {
    const response = await fetch("/api/voices");
    if (!response.ok) throw new Error("音色列表加载失败，请确认服务已更新后刷新");
    const {voices, default: defaultVoice} = await response.json();
    for (const name of ["inbound_voice", "outbound_voice"]) {
      element(name).replaceChildren(...voices.map(voice => new Option(voice, voice)), new Option("跟随说话人（每次会话复刻一次）", "clone_once"), new Option("跟随说话人（逐段更新）", "clone_always"));
      element(name).value = defaultVoice;
    }
    for (const name of ["region", "mode", "inbound_voice", "outbound_voice"]) {
      if ([...element(name).options].some(option => option.value === preferences[name])) element(name).value = preferences[name];
    }
    await refresh();
    element("start").disabled = false;
  } catch (error) { element("error").textContent = error.message; }
}
initialize();

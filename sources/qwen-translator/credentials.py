import subprocess
import hashlib


APPLICATION_ID = "qwen-live-translate"


def credential(action, region, value=""):
    if region not in {"mainland", "intl"}:
        raise ValueError("Invalid key region")
    arguments = ["secret-tool", action]
    if action == "store":
        if not value.strip():
            raise ValueError("请输入 API Key")
        arguments += ["--label=Qwen LiveTranslate API Key"]
    elif action not in {"lookup", "clear"}:
        raise ValueError("Invalid key operation")
    result = subprocess.run(
        arguments + ["application", APPLICATION_ID, "region", region],
        input=value if action == "store" else None, text=True, capture_output=True, timeout=30,
    )
    if result.returncode and not (action in {"lookup", "clear"} and result.returncode == 1 and not result.stderr.strip()):
        raise RuntimeError("系统密钥库操作失败，请确认登录密钥库已解锁。")
    return result.stdout.rstrip("\n") if action == "lookup" else ""


def text_credential(action, endpoint, value=""):
    """Separate text-provider keys by endpoint; never reuse the voice API key."""
    if action not in {"lookup", "store", "clear"}:
        raise ValueError("Invalid key operation")
    if action == "store" and not value.strip():
        raise ValueError("请输入文本模型 API Key")
    identity = hashlib.sha256(endpoint.strip().rstrip('/').encode()).hexdigest()
    args = ["secret-tool", action]
    if action == "store":
        args += ["--label=Meeting Translator Text API Key"]
    result = subprocess.run(args + ["application", APPLICATION_ID, "text_endpoint", identity],
                            input=value if action == "store" else None, text=True, capture_output=True, timeout=30)
    if result.returncode and not (action in {"lookup", "clear"} and result.returncode == 1 and not result.stderr.strip()):
        raise RuntimeError("文本模型密钥库操作失败，请确认密钥库已解锁。")
    return result.stdout.rstrip("\n") if action == "lookup" else ""


def nas_credential(action, service, value=""):
    """Arr keys have a separate keychain namespace from text and voice APIs."""
    from nas_api import service_id
    return text_credential(action, 'nas-service:' + service_id(service), value)

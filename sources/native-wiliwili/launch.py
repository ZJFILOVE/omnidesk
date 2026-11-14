#!/usr/bin/env python3
"""Owns the companion and optional bridge; kills both when the native client exits."""
import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

def healthy(port,path,headers):
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=1)
    try:
        conn.request('GET',path,headers=headers)
        response=conn.getresponse()
        response.read()
        return response.status==200
    except OSError: return False
    finally: conn.close()

def main():
    parser=argparse.ArgumentParser(description='wiliwili AI · 本机 GPT-6 Luna')
    parser.add_argument('--root',default=str(Path(__file__).resolve().parent))
    parser.add_argument('--bv')
    parser.add_argument('--cid')
    parser.add_argument('--ai-test-media')
    parser.add_argument('--fixture')
    parser.add_argument('--diagnostics')
    parser.add_argument('--version',action='store_true')
    args,extra=parser.parse_known_args()
    root=Path(args.root)
    binary=root/'bin/wiliwili'
    if not binary.exists(): binary=root/'build/wiliwili'
    if args.version: return subprocess.call([str(binary),'--version'])
    config=Path.home()/'.config/bili-helper-codex/config.json'
    if config.stat().st_mode&0o077: raise SystemExit('本机接口配置须为 0600 权限')
    settings=json.loads(config.read_text())
    if settings.get('model')!='gpt-6-luna' or settings.get('port')!=43188: raise SystemExit('本机接口配置与预期不符')
    env=os.environ.copy()
    env['WILI_AI_TOKEN']=secrets.token_urlsafe(40)
    # Each native process owns its companion; another user's player is untouched.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        companion_port=listener.getsockname()[1]
    env['WILI_AI_PORT']=str(companion_port)
    owned=[]
    client=None
    temp=None
    def stop(*_):
        if client and client.poll() is None: client.terminate()
        for process in reversed(owned):
            if process.poll() is None: process.terminate()
        for process in reversed(owned):
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
    signal.signal(signal.SIGINT,stop)
    signal.signal(signal.SIGTERM,stop)
    try:
        if not healthy(43188,'/health',{'Authorization':'Bearer '+settings['token']}):
            bridge=root/'bridge.py'
            if not bridge.is_file(): raise SystemExit('发行文件缺失：bridge.py')
            owned.append(subprocess.Popen([sys.executable,str(bridge),'--config',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
            for _ in range(30):
                if healthy(43188,'/health',{'Authorization':'Bearer '+settings['token']}): break
                time.sleep(.1)
            else: raise SystemExit('现有本机模型接口无法启动')
        service=[sys.executable,str(root/'service.py'),'--port',str(companion_port)]
        if args.fixture:
            if not args.ai_test_media: raise SystemExit('测试资料须与本地测试媒体一起使用')
            temp=tempfile.TemporaryDirectory(prefix='wiliwili-ai-test-')
            env['XDG_CONFIG_HOME']=str(Path(temp.name)/'config')
            # Exercise desktop density without borrowing the user's cookies or preferences.
            test_config=Path(env['XDG_CONFIG_HOME'])/'wiliwili/wiliwili_config.json'
            test_config.parent.mkdir(parents=True,exist_ok=True)
            test_config.write_text(json.dumps({'setting':{'app_ui_scale':'1080p','search_tv_mode':False}}))
            service+=['--fixture',str(Path(args.fixture).resolve()),'--state-dir',str(Path(temp.name)/'state')]
        if args.diagnostics:
            if not (args.ai_test_media or args.bv): raise SystemExit('诊断仅用于显式视频复测')
            env['WILI_AI_DIAGNOSTICS']=str(Path(args.diagnostics).resolve())
        owned.append(subprocess.Popen(service,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        for _ in range(30):
            if healthy(companion_port,'/health',{'X-Wili-Auth':env['WILI_AI_TOKEN']}): break
            if owned[-1].poll() is not None: raise SystemExit('本机助手启动失败')
            time.sleep(.1)
        else: raise SystemExit('本机助手启动超时')
        binary=root/'bin/wiliwili'
        if not binary.exists(): binary=root/'build/wiliwili'
        command=[str(binary),*extra]
        if args.bv: command+=['--bv',args.bv]
        if args.cid: command+=['--cid',args.cid]
        if args.ai_test_media: command+=['--ai-test-media',str(Path(args.ai_test_media).resolve())]
        cwd=root if (root/'resources').exists() else root/'build'
        if args.diagnostics: command=['stdbuf','-oL','-eL',*command]
        native_env=env.copy()
        # Hyprland 0.56.x hides native Wayland IME popups in fullscreen.
        # XWayland keeps Fcitx's candidate window visible; honor an explicit backend.
        if native_env.get('HYPRLAND_INSTANCE_SIGNATURE') and native_env.get('WAYLAND_DISPLAY') and native_env.get('DISPLAY'):
            native_env.setdefault('SDL_VIDEODRIVER','x11')
        client=subprocess.Popen(command,cwd=cwd,env=native_env)
        return client.wait()
    finally:
        stop()
        if temp: temp.cleanup()

if __name__=='__main__': sys.exit(main())

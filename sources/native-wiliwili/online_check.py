#!/usr/bin/env python3
"""Read actual Bilibili sources without importing browser login credentials."""
import json
import os
from pathlib import Path
from service import Collector, Transport, load_cookie

root=Path(__file__).resolve().parent
collector=Collector(Transport(),load_cookie(Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'wiliwili'))
results=[]
popular=collector.fetch('/x/web-interface/popular',{'pn':1,'ps':20})
candidates=[v['bvid'] for v in popular['list'] if 180<=v.get('duration',0)<=1200][:2]
for bvid in candidates:
    try:
        meta=collector.fetch('/x/web-interface/view',{'bvid':bvid})
        page=meta['pages'][0]
        video=dict(bvid=bvid,cid=page['cid'],p=1,duration=page['duration'],aid=meta['aid'],title=meta['title'])
        context=collector.collect(video)
        results.append(dict(video=context['video'],sources={key:len(context[key]) for key in ['subtitles','danmaku','comments']},warnings=context['warnings']))
        (root/'validation'/f'online-{bvid}.json').write_text(json.dumps(context,ensure_ascii=False,indent=2))
    except Exception as error:
        results.append(dict(bvid=bvid,error=str(error)))
    print(json.dumps(results[-1],ensure_ascii=False),flush=True)
(root/'validation/online-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

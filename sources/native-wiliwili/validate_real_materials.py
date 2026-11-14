#!/usr/bin/env python3
"""Real authenticated metadata/subtitle collection, no model or fixture inputs."""
import json
from pathlib import Path
from service import Collector, Transport, load_cookie

ROOT=Path(__file__).resolve().parent
results=[]
collector=Collector(Transport(),load_cookie(Path.home()/'.config/wiliwili'))
for bvid in ['BV1heam6TExz','BV1Eb411u7Fw','BV1sK411s7zq','BV1Cg411j76F']:
    row={'bvid':bvid,'evidence':'real_authenticated_service'}
    try:
        meta=collector.fetch('/x/web-interface/view',{'bvid':bvid})
        pages=meta.get('pages') or []
        row['parts']=len(pages)
        page=pages[0]
        video={'bvid':bvid,'cid':page['cid'],'duration':page['duration'],'p':page['page']}
        material=collector.collect(video)
        row.update(subtitle_lines=len(material['subtitles']),danmaku_lines=len(material['danmaku']),comments=len(material['comments']),warnings=material['warnings'])
        row['result']='pass' if material['subtitles'] else 'no_subtitles'
    except Exception as error:row.update(result='unavailable',error=type(error).__name__)
    results.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    (ROOT/'validation/release-3.0-real-materials.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')

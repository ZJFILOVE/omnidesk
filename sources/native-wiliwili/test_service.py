import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from service import (ALGORITHM, MODEL, Cancelled, Collector, CompanionServer, Session, Store,
                     TaskError, Transport, compact_subtitles, continuous_batches, effective_ads,
                     parse_ads, parse_danmaku_segment, parse_sponsorblock_segments,
                     parse_subtitle_body, prioritize_batches, select_relevant_subtitles,
                     subtitle_track_rank)

VIDEO=dict(bvid='BVAItest0001',cid=1,p=1,aid=1,duration=200,title='测试',desc='介绍')
LINES=[dict(start=0,end=20,text='普通内容'),dict(start=20,end=40,text='Sponsored by QuantaSprout. Use our discount link to buy.'),dict(start=40,end=200,text='正文内容')]
CONTEXT=dict(video=VIDEO,subtitles=LINES,danmaku=[dict(time=5,text='这不是广告')],comments=[],warnings=[])
AD=dict(start=20,end=40,confidence=.9,source='model',product_name='QuantaSprout',ad_content='赞助')

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.sessions=[]
    def tearDown(self):
        for session in self.sessions:
            session.cancel()
            if session.thread.is_alive(): session.thread.join(1)
        self.temp.cleanup()
    def session(self,context=None,complete=None):
        context=context or copy.deepcopy(CONTEXT)
        session=Session(context['video'],dict(port=43188,token='not-a-secret-test-value'),self.store,Path(self.temp.name),
                        collect=lambda video:context,complete=complete or (lambda messages:'{"ads":[]}'))
        session.context=context
        self.sessions.append(session)
        return session
    def test_general_chat_without_video_material_and_followup(self):
        calls=[]
        session=self.session(complete=lambda messages:(calls.append(messages) or '可以讨论一般问题。'))
        session.context=None  # Collection failure must not block general questions.
        session.ask('解释一下勾股定理')
        self.assertEqual(calls[0][-1]['content'], '解释一下勾股定理')
        self.assertIn('与视频无关', calls[0][0]['content'])
        self.assertIn('不编造视频内容', calls[0][0]['content'])
        self.assertIn('并非问题范围限制', calls[0][1]['content'])
        self.assertEqual(json.loads(calls[0][1]['content'].split('\n',1)[1])['subtitles'], [])
        session.ask('给我一个例子')
        self.assertEqual(calls[1][2:4], [dict(role='user',content='解释一下勾股定理'),
                                       dict(role='assistant',content='可以讨论一般问题。')])
        self.assertFalse(session.state['busy'])

    def test_contiguous_batches_cover_every_line(self):
        lines=[dict(start=i,end=i+1,text=str(i)*20) for i in range(1200)]
        batches=list(continuous_batches(lines,max_lines=100,max_chars=6000))
        self.assertEqual({line['start'] for batch in batches for line in batch},set(range(1200)))
        for a,b in zip(batches,batches[1:]): self.assertLessEqual(b[0]['start'],a[-1]['start'])
    def test_bilibili_protobuf_progress_is_milliseconds(self):
        def varint(value):
            out=bytearray()
            while value>=128: out.append((value&127)|128);value>>=7
            out.append(value);return bytes(out)
        text='字幕是主证，弹幕旁证'.encode()
        row=b'\x10'+varint(20500)+b'\x3a'+varint(len(text))+text
        data=b'\x0a'+varint(len(row))+row
        self.assertEqual(parse_danmaku_segment(data),[dict(time=20.5,text=text.decode())])
    def test_malformed_protobuf_fails_without_fabricating_danmaku(self):
        for data in [b'\x0a\xff',b'\x00',b'\x0a\x07\x10']:
            with self.assertRaises(TaskError): parse_danmaku_segment(data)
    def test_unknown_english_brand_does_not_need_vocabulary(self):
        prompts=[]
        session=self.session(complete=lambda messages:(prompts.append(messages) or json.dumps({'ads':[AD]})))
        session.detect()
        self.assertEqual(session.snapshot()['ads'][0]['start'],20)
        self.assertIn('QuantaSprout',prompts[0][1]['content'])
        self.assertIn('未知品牌',prompts[0][0]['content'])
    def test_adjacent_confidence_is_independent(self):
        rows=[{**AD,'start':0,'end':98,'confidence':.4},{**AD,'start':100,'end':110,'confidence':.9}]
        result=parse_ads(json.dumps({'ads':rows}),200)
        self.assertEqual([(a['start'],a['end'],a['confidence']) for a in result],[(0,98,.4),(100,110,.9)])
    def test_no_subtitles_is_not_negative_cached(self):
        context=copy.deepcopy(CONTEXT); context['subtitles']=[]
        session=self.session(context,complete=lambda _:self.fail('model must not run'))
        session.detect()
        self.assertIsNone(self.store.read('detect-',session.cache_key,None))
        self.assertIn('无字幕',session.snapshot()['status'])
    def test_three_danmaku_cannot_confirm_an_ad(self):
        context=copy.deepcopy(CONTEXT)
        context['danmaku']=[dict(time=i,text='广告') for i in range(3)]
        calls=[]
        session=self.session(context,lambda messages:(calls.append(messages) or '{"ads":[]}'))
        session.detect()
        self.assertEqual(len(calls),1)
        self.assertEqual(session.snapshot()['ads'],[])
    def test_format_error_not_cached(self):
        for response in ['bad','{}','{"ads":[{"start":50,"end":1}]}','{"ads":[{"start":20,"end":40,"confidence":2}]}']:
            with self.subTest(response=response):
                session=self.session(complete=lambda _,response=response:response)
                with self.assertRaises(TaskError): session.detect()
                self.assertIsNone(self.store.read('detect-',session.cache_key,None))
    def test_bad_item_next_to_good_item_still_not_cached(self):
        session=self.session(complete=lambda _:json.dumps({'ads':[AD,{'start':'nan','end':80}]}))
        with self.assertRaises(TaskError): session.detect()
        self.assertIsNone(self.store.read('detect-',session.cache_key,None))
    def test_model_failure_not_cached(self):
        def fail(_): raise TaskError('model failed')
        session=self.session(complete=fail)
        with self.assertRaises(TaskError): session.detect()
        self.assertIsNone(self.store.read('detect-',session.cache_key,None))
    def test_partial_batch_failure_not_cached(self):
        context=copy.deepcopy(CONTEXT)
        context['subtitles']=[dict(start=i/10,end=(i+1)/10,text='正文'*20) for i in range(1300)]
        calls=[]
        def model(_):
            calls.append(1)
            if len(calls)==2: raise TaskError('failed')
            return '{"ads":[]}'
        session=self.session(context,model)
        with self.assertRaises(TaskError): session.detect()
        self.assertIsNone(self.store.read('detect-',session.cache_key,None))
    def test_only_successful_full_empty_is_cached(self):
        session=self.session(); session.detect()
        cached=self.store.read('detect-',session.cache_key,None)
        self.assertTrue(cached['confirmed']); self.assertEqual(cached['ads'],[])
    def test_cache_identity(self):
        session=self.session()
        self.assertIn('BVAItest0001:1:'+MODEL+':'+ALGORITHM,session.cache_key)
        context=copy.deepcopy(CONTEXT); context['video']={**VIDEO,'cid':2}
        self.assertNotEqual(self.session(context).cache_key,session.cache_key)
    def test_cache_positive_replays(self):
        session=self.session(complete=lambda _:json.dumps({'ads':[AD]})); session.detect()
        second=self.session(complete=lambda _:self.fail('cache miss')); second.detect()
        self.assertEqual(second.snapshot()['ads'][0]['end'],40)
    def test_summary_does_not_promote_low_confidence(self):
        low={**AD,'start':0,'end':98,'confidence':.4}
        chapter={**AD,'start':0,'end':110,'confidence':.8,'source':'summary'}
        self.assertEqual(effective_ads([low],[chapter],[],[]),[low])
    def test_summary_supplements_only_separate_interval(self):
        chapter={**AD,'start':100,'end':110,'confidence':.8,'source':'summary'}
        self.assertEqual(len(effective_ads([AD],[chapter],[],[])),2)
    def test_human_exclusion_always_wins(self):
        self.assertEqual(effective_ads([AD],[AD],[AD],[dict(start=20,end=40)]),[])
    def test_exclusion_splits_without_increasing_confidence(self):
        result=effective_ads([{**AD,'start':0,'end':98,'confidence':.4}],[],[],[dict(start=20,end=40)])
        self.assertEqual([(a['start'],a['end'],a['confidence']) for a in result],[(0,20,.4),(40,98,.4)])
    def test_corrections_persist_and_survive_re_detection(self):
        session=self.session(complete=lambda _:json.dumps({'ads':[AD]})); session.detect()
        session.command('not_ad',dict(start=20,end=40)); self.assertEqual(session.snapshot()['ads'],[])
        second=self.session(); second.detect(); self.assertEqual(second.snapshot()['ads'],[])
    def test_manual_add_and_later_correction(self):
        session=self.session(); session.command('manual',dict(start=50,end=70))
        self.assertEqual(session.snapshot()['ads'][0]['source'],'manual')
        session.command('not_ad',dict(start=50,end=70)); self.assertEqual(session.snapshot()['ads'],[])
    def test_manual_subrange_keeps_surrounding_human_exclusions(self):
        session=self.session(complete=lambda _:json.dumps({'ads':[AD]})); session.detect()
        session.command('not_ad',dict(start=20,end=40))
        session.command('manual',dict(start=25,end=30))
        self.assertEqual([(a['start'],a['end'],a['source']) for a in session.snapshot()['ads']],[(25,30,'manual')])
    def test_null_bilibili_fields_do_not_abort_collection(self):
        class FakeTransport:
            def request(self,*args,**kwargs): return b'<i/>'
        collector=Collector(FakeTransport(),'')
        def fetch(path,*args,**kwargs):
            if path=='/x/web-interface/view': return dict(bvid=VIDEO['bvid'],pages=[dict(cid=1,duration=200,page=1)],owner=None)
            if path.endswith('/detail'): return dict(Card=None,Tags=None)
            if '/player/' in path: return dict(subtitle=None)
            return dict(top=None,replies=None,top_replies=None)
        collector.fetch=fetch
        result=collector.collect(VIDEO)
        self.assertEqual(result['subtitles'],[]); self.assertEqual(result['comments'],[])
    def test_invalid_manual_interval(self):
        session=self.session()
        for start,end in [(30,20),(-1,30),(10,201),(10,11)]:
            with self.assertRaises(TaskError): session.command('manual',dict(start=start,end=end))
    def test_late_result_after_cancel_not_published_or_cached(self):
        entered,release=threading.Event(),threading.Event()
        def model(_): entered.set(); release.wait(2); return json.dumps({'ads':[AD]})
        session=self.session(complete=model); session.start()
        self.assertTrue(entered.wait(1)); session.cancel(); release.set(); session.thread.join(1)
        self.assertEqual(session.snapshot()['ads'],[])
        self.assertIsNone(self.store.read('detect-',session.cache_key,None))
    def test_total_duration_gate_independent_of_position(self):
        for duration,expected in [(179,0),(180,1),(200,1)]:
            context=copy.deepcopy(CONTEXT); context['video']={**VIDEO,'duration':duration,'cid':duration}; context['subtitles']=LINES[:2]
            calls=[]; session=self.session(context,lambda _:(calls.append(1) or '{"ads":[]}'))
            session.start()
            deadline=time.monotonic()+1
            while session.snapshot()['busy'] and time.monotonic()<deadline: time.sleep(.005)
            self.assertEqual(len(calls),expected)
    def test_sources_and_links_delivered_to_model(self):
        context=copy.deepcopy(CONTEXT)
        context['video']={**VIDEO,'upData':dict(name='UP',sign='签名'),'tags':['标签']}
        context['comments']=[dict(text='置顶',pinned=True,links={'https://example.com':{'title':'优惠'}})]
        calls=[]; session=self.session(context,lambda messages:(calls.append(messages) or '{"ads":[]}')); session.detect()
        self.assertIn('优惠',calls[0][1]['content']); self.assertIn('签名',calls[0][1]['content'])
    def test_cancel_prevents_further_commands(self):
        session=self.session(); session.cancel()
        with self.assertRaises(Cancelled): session.command('summary',{})
    def test_failed_initial_collection_can_be_retried(self):
        session=self.session()
        calls=[]
        def collect(video):
            calls.append(video)
            if len(calls)==1: raise TaskError('temporary failure')
            return copy.deepcopy(CONTEXT)
        session.collector=collect
        session.start()
        deadline=time.monotonic()+2
        while session.snapshot()['busy'] and time.monotonic()<deadline: time.sleep(.005)
        self.assertTrue(session.thread.is_alive())
        session.command('detect',dict(refresh=True))
        while session.snapshot()['busy'] and time.monotonic()<deadline: time.sleep(.005)
        self.assertEqual(len(calls),2)
        self.assertEqual(session.snapshot()['status'],'模型已确认无广告')
    def test_retry_reloads_independent_client_login(self):
        directory=Path(self.temp.name)
        config=directory/'wiliwili_config.json'
        cookies=[]
        session=Session(VIDEO,dict(port=43188,token='not-a-secret-test-value'),self.store,directory,complete=lambda _:'{"ads":[]}')
        self.sessions.append(session)
        def collect(collector,video):
            cookies.append(collector.cookie)
            return copy.deepcopy(CONTEXT)
        with patch.object(Collector,'collect',collect):
            config.write_text(json.dumps(dict(cookie={'DedeUserID':'0'})))
            session.collector(VIDEO)
            config.write_text(json.dumps(dict(cookie={'DedeUserID':'123'})))
            session.collector(VIDEO)
        self.assertEqual(cookies,['DedeUserID=0','DedeUserID=123'])
    def test_summary_no_subtitles_never_adds_ads(self):
        context=copy.deepcopy(CONTEXT); context['subtitles']=[]
        session=self.session(context,lambda _:json.dumps(dict(summary='资料不足',segments=[dict(start=20,end=40,label='广告',type='ad')])))
        session.summarize(); self.assertEqual(session.snapshot()['ads'],[])

    def test_sponsorblock_parser_and_reputation_scoring(self):
        payload = json.dumps([
            {"videoID": "BVother00001", "segments": [{"segment": [10, 30], "cid": "1", "votes": 5, "locked": 1}]},
            {"videoID": VIDEO['bvid'], "segments": [
                {"segment": [15, 35], "cid": "1", "UUID": "u1", "category": "sponsor", "actionType": "skip", "votes": 0, "locked": 1, "videoDuration": 200},
                {"segment": [90, 110], "cid": 1, "UUID": "u2", "category": "sponsor", "actionType": "skip", "votes": 3, "locked": 0, "videoDuration": 200},
                {"segment": [120, 140], "cid": "1", "UUID": "u3", "category": "sponsor", "actionType": "skip", "votes": -2, "locked": 0, "videoDuration": 200},
                {"segment": [150, 170], "cid": "99", "UUID": "u4", "category": "sponsor", "actionType": "skip", "votes": 5, "locked": 1, "videoDuration": 200},
            ]}
        ])
        segs = parse_sponsorblock_segments(payload, VIDEO['bvid'], 1, 200)
        self.assertEqual([(s['start'], s['end'], s['confidence']) for s in segs], [(15.0, 35.0, 0.98), (90.0, 110.0, 0.95)])

    def test_smart_fallback_skips_llm_when_high_reputation_sponsorblock_hits(self):
        calls = []
        sb = [dict(start=20, end=40, confidence=0.98, product_name='空降助手 · 已锁定', ad_content='bsbsb.top', source='sponsorblock', votes=5, locked=True)]
        session = Session(VIDEO, dict(port=43188, token='not-a-secret-test-value'), self.store, Path(self.temp.name),
                          collect=lambda v: copy.deepcopy(CONTEXT),
                          complete=lambda m: (calls.append(m) or '{"ads":[]}'),
                          sponsorblock_fetch=lambda v: sb)
        self.sessions.append(session)
        session.start()
        deadline = time.monotonic() + 2
        while session.snapshot()['busy'] and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(calls), 0)
        self.assertEqual(len(session.snapshot()['ads']), 1)
        self.assertEqual(session.snapshot()['ads'][0]['source'], 'sponsorblock')

    def test_prioritized_progressive_batches_stream_before_all_complete(self):
        lines = [dict(start=i, end=i+1, text='普通内容填充文本' * 4) for i in range(260)]
        lines[235] = dict(start=235, end=236, text='Sponsored by QuantaSprout buy now')
        context = dict(video={**VIDEO, 'duration': 260, 'position': 230}, subtitles=lines, danmaku=[], comments=[], warnings=[])
        batches = list(continuous_batches(lines))
        self.assertGreaterEqual(len(batches), 2)
        ordered = prioritize_batches(batches, 230)
        self.assertGreaterEqual(ordered[0][-1]['end'], 230)

        batch1_done, release_batch2 = threading.Event(), threading.Event()
        call_idx = [0]
        def complete(_):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return json.dumps({'ads': [dict(start=235, end=239, confidence=0.92, product_name='QuantaSprout', ad_content='promo')]})
            batch1_done.set()
            release_batch2.wait(2)
            return '{"ads":[]}'

        session = Session(context['video'], dict(port=43188, token='not-a-secret-test-value'), self.store, Path(self.temp.name),
                          collect=lambda v: context, complete=complete)
        self.sessions.append(session)
        session.start()
        self.assertTrue(batch1_done.wait(2))
        snap = session.snapshot()
        self.assertTrue(snap['busy'])
        self.assertEqual(len(snap['ads']), 1)
        self.assertEqual(snap['ads'][0]['start'], 235)
        release_batch2.set()

    def test_engine_mode_switching_filters_sources(self):
        sb = [dict(start=50, end=70, confidence=0.98, product_name='空降', ad_content='sb', source='sponsorblock', votes=4, locked=True)]
        session = self.session(complete=lambda _: json.dumps({'ads': [AD]}))
        session.sponsor_ads = sb
        session.sponsor_checked = True
        session.detect()
        self.assertEqual({a['source'] for a in session.snapshot()['ads']}, {'sponsorblock', 'model'})
        session.command('set_engine_mode', {'mode': 'sponsorblock_only'})
        self.assertEqual([a['source'] for a in session.snapshot()['ads']], ['sponsorblock'])
        session.command('set_engine_mode', {'mode': 'ai_only'})
        self.assertEqual([a['source'] for a in session.snapshot()['ads']], ['model'])

    def test_subtitle_track_ranking_millisecond_normalization_and_compaction(self):
        tracks = [
            {'lan': 'en', 'lan_doc': 'English', 'subtitle_url': '//aisubtitle.hdslb.com/en.json'},
            {'lan': 'ai-zh', 'lan_doc': '中文（自动生成）', 'type': 1, 'subtitle_url': '//aisubtitle.hdslb.com/ai.json'},
            {'lan': 'zh-CN', 'lan_doc': '中文（简体）', 'subtitle_url': '//aisubtitle.hdslb.com/zh.json'},
        ]
        ranked = [t['lan'] for t in sorted(tracks, key=subtitle_track_rank)]
        self.assertEqual(ranked, ['zh-CN', 'ai-zh', 'en'])
        ms_body = [
            {'from': 1000, 'to': 2000, 'content': '今天'},
            {'from': 2100, 'to': 3000, 'content': '今天'},
            {'from': 3200, 'to': 5000, 'content': '我们来聊聊散热器。'},
        ]
        cleaned, raw_count = parse_subtitle_body(ms_body, duration=200)
        self.assertEqual(raw_count, 3)
        self.assertEqual(len(cleaned), 2)
        self.assertAlmostEqual(cleaned[0]['start'], 1.0)
        self.assertAlmostEqual(cleaned[0]['end'], 3.0)
        compacted = compact_subtitles(cleaned)
        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0]['text'], '今天我们来聊聊散热器。')

    def test_collector_subtitle_info_tracks_and_login_hint(self):
        class FakeTransport:
            def request(self, url, *args, **kwargs):
                if 'zh.json' in url:
                    return json.dumps({'body': [{'from': 1, 'to': 4, 'content': '正式中文字幕'}]}).encode()
                if 'en.json' in url:
                    return json.dumps({'body': [{'from': 1, 'to': 4, 'content': 'English sub'}]}).encode()
                return b'<i/>'

        collector = Collector(FakeTransport(), 'SESSDATA=valid')
        def fetch(path, *args, **kwargs):
            if path == '/x/web-interface/view':
                return dict(bvid=VIDEO['bvid'], pages=[dict(cid=1, duration=200, page=1)], owner=None)
            if path.endswith('/detail'):
                return dict(Card=None, Tags=None)
            if '/player/' in path:
                return dict(login_mid=12345, subtitle=dict(subtitles=[
                    dict(lan='en', lan_doc='English', subtitle_url='//aisubtitle.hdslb.com/en.json'),
                    dict(lan='zh-CN', lan_doc='中文（简体）', subtitle_url='//aisubtitle.hdslb.com/zh.json'),
                ]))
            return dict(top=None, replies=None, top_replies=None)
        collector.fetch = fetch
        result = collector.collect(VIDEO)
        info = result['subtitle_info']
        self.assertTrue(info['available'])
        self.assertEqual(info['status'], 'available')
        self.assertEqual(info['selected_lan'], 'zh-CN')
        self.assertEqual(info['track_count'], 2)

        # Logged out + no tracks -> explicit login_required status
        anon = Collector(FakeTransport(), '')
        def fetch_empty(path, *args, **kwargs):
            if path == '/x/web-interface/view':
                return dict(bvid=VIDEO['bvid'], pages=[dict(cid=1, duration=200, page=1)], owner=None)
            if path.endswith('/detail'):
                return dict(Card=None, Tags=None)
            if '/player/' in path:
                return dict(login_mid=0, subtitle=dict(subtitles=[]))
            return dict(top=None, replies=None, top_replies=None)
        anon.fetch = fetch_empty
        anon_res = anon.collect(VIDEO)
        self.assertFalse(anon_res['subtitle_info']['available'])
        self.assertEqual(anon_res['subtitle_info']['status'], 'login_required')

    def test_select_subtitle_track_and_qa_playback_position(self):
        lines = [dict(start=i * 10, end=i * 10 + 8, text=f'第{i}句内容' + (' 关键参数42' if i == 150 else '')) for i in range(400)]
        picked = select_relevant_subtitles(lines, question='关键参数是多少', playback_time=3050.0, max_lines=100)
        self.assertLessEqual(len(picked), 100)
        self.assertTrue(any('关键参数42' in line['text'] for line in picked))
        self.assertTrue(any(3000 <= line['start'] <= 3095 for line in picked))

        calls = []
        session = self.session(complete=lambda m: (calls.append(m) or '当前正在讲普通内容。〔字幕 00:00–00:20〕'))
        session.ask('刚才这里讲什么', position=25.0)
        payload = json.loads(calls[0][1]['content'].split('\n', 1)[1])
        self.assertEqual(payload['current_position'], '00:25')

        session.context['subtitle_tracks'] = [
            dict(lan='zh-CN', lan_doc='中文', ai=False, url='https://aisubtitle.hdslb.com/zh.json'),
            dict(lan='en', lan_doc='English', ai=False, url='https://aisubtitle.hdslb.com/en.json'),
        ]
        with patch.object(session.transport, 'request', return_value=json.dumps({'body': [{'from': 5, 'to': 9, 'content': 'Hello world'}]}).encode()):
            session.select_subtitle({'index': 1})
        snap = session.snapshot()
        self.assertEqual(snap['subtitle_info']['selected_lan'], 'en')
        self.assertEqual(snap['sources']['subtitles'], 1)

    def test_summarize_synthesizes_long_video_and_handles_missing_context(self):
        lines = [dict(start=i * 15, end=i * 15 + 14, text=f'这是第{i}个独立完整句子。') for i in range(250)]
        ctx = dict(video={**VIDEO, 'duration': 4000}, subtitles=lines, danmaku=[], comments=[], warnings=[])
        calls = []
        def complete(messages):
            calls.append(messages)
            if '整理助手' in messages[0]['content']:
                return '## 综合总结\n整合后的要点。'
            return json.dumps({'summary': f'批次{len(calls)}摘要', 'segments': []})
        session = self.session(context=ctx, complete=complete)
        session.summarize()
        self.assertEqual(session.snapshot()['summary'], '## 综合总结\n整合后的要点。')

if __name__=='__main__': unittest.main(verbosity=2)

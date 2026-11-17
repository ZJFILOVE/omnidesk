import importlib.util
from pathlib import Path
import sys
import types
import pytest

from omnidesk_sdk.media_markup import image_markdown

ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    path = ROOT / relative
    package_name = "image_test_" + path.parent.name.replace("-", "_")
    package = types.ModuleType(package_name)
    package.__path__ = [str(path.parent)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(package_name + "." + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_safe_image_markup():
    assert image_markdown('javascript:evil()', 'bad') == '[图片]'
    assert image_markdown('//img.example.org/p.jpg', '图', base_url='https://example.org/') == '![图](<https://img.example.org/p.jpg>)'
    assert '\\]' in image_markdown('https://example.org/p.jpg', 'a]b')


@pytest.mark.parametrize('path,function', [
    ('plugins/plugin-linuxdo/html_parser.py', 'parse_cooked_to_paragraphs'),
    ('plugins/plugin-shuiyuan/shuiyuan_html.py', 'cooked_to_paragraphs'),
    ('plugins/plugin-v2ex/v2ex_parser.py', 'parse_rich_html_to_paragraphs'),
])
def test_html_images_survive_into_readable_blocks(path, function):
    module = load(path)
    if not hasattr(module, function):
        function = 'parse_cooked_to_paragraphs'
    blocks = getattr(module, function)('<p>before<img src="https://example.org/p.jpg" alt="图注">after</p>')
    body = '\n'.join(block.original for block in blocks)
    assert '![图注](<https://example.org/p.jpg>)' in body
    assert 'before' in body and 'after' in body


def test_web_extract_keeps_more_than_120_paragraphs_and_image():
    module = load('plugins/plugin-chrome-web/html_extract.py')
    html = ''.join(f'<p>paragraph {i}</p>' for i in range(301)) + '<p><img src="/p.jpg" alt="图"></p>'
    result = module.extract_from_html(html, base_url='https://example.org/')
    assert len(result['paragraphs']) == 302
    assert 'paragraph 300' in result['text'] and 'https://example.org/p.jpg' in result['text']


def test_x_photos_video_posters_and_comment_attachments_are_visible():
    module = load('plugins/plugin-x/x_parser.py')
    tweet = module.ParsedTweet(id='1', text='正文', media=[
        module.XMediaAttachment(type='photo', url='https://pbs.twimg.com/media/photo.jpg', alt_text='现场图'),
        module.XMediaAttachment(type='video', url='https://video.twimg.com/video.mp4', poster_url='https://pbs.twimg.com/poster.jpg'),
    ])
    blocks = module.split_tweet_text_to_paragraphs(tweet)
    images = [block for block in blocks if block.kind == 'image']
    assert [image.image_url for image in images] == ['https://pbs.twimg.com/media/photo.jpg', 'https://pbs.twimg.com/poster.jpg']
    assert '现场图' == images[0].alt
    assert '![现场图]' in module._tweet_to_comment_node(tweet).body
    assert any('video.mp4' in block.original for block in blocks)

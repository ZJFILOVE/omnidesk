"""Offline tests for the journals plugin and its parsers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import PluginContext  # noqa: E402
from omnidesk_sdk.http import HttpResponse  # noqa: E402
from plugin import JournalPlugin, create_plugin  # noqa: E402

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>arXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2401.01234v2</id>
    <published>2026-01-02T03:04:05Z</published>
    <title>Reliable Agents
      for Scientific Reading</title>
    <summary>We study multi-agent reading of scientific papers.</summary>
    <author><name>Alice Zhang</name></author>
    <author><name>Bob Li</name></author>
    <link href="http://arxiv.org/abs/2401.01234v2" rel="alternate"/>
    <category term="cs.AI"/>
    <arxiv:doi>10.1000/example</arxiv:doi>
  </entry>
</feed>
"""

PUBMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <Journal>
          <Title>Nature Machine Intelligence</Title>
          <JournalIssue><PubDate><Year>2026</Year><Month>Nov</Month></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Foundation models for <i>biology</i></ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Models are improving.</AbstractText>
          <AbstractText Label="RESULTS">We report a result.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Chen</LastName><ForeName>Wei</ForeName></Author>
        </AuthorList>
      </Article>
      <KeywordList><Keyword>foundation models</Keyword></KeywordList>
    </MedlineCitation>
    <ArticleIdList>
      <ArticleId IdType="doi">10.1038/s42256-026-00001</ArticleId>
    </ArticleIdList>
  </PubmedArticle>
</PubmedArticleSet>
"""

CROSSREF_JSON = json.dumps(
    {
        "status": "ok",
        "message": {
            "DOI": "10.1038/s42256-026-00001",
            "title": ["A foundation model for biology"],
            "author": [
                {"given": "Wei", "family": "Chen"},
                {"given": "Alice", "family": "Zhang"},
            ],
            "container-title": ["Nature Machine Intelligence"],
            "published": {"date-parts": [[2026, 11, 3]]},
            "URL": "https://doi.org/10.1038/s42256-026-00001",
            "abstract": "<jats:p>We present a biology foundation model.</jats:p>",
            "subject": ["AI", "Biology"],
            "is-referenced-by-count": 12,
        },
    }
)

ARTICLE_HTML = """<!doctype html>
<html><head><title>Fallback title</title>
<meta property="og:title" content="A Blog Post"/>
<meta property="og:image" content="https://example.com/cover.png"/>
</head><body><article>
<h1>A Blog Post</h1>
<p>First paragraph of the article.</p>
<h2>Details</h2>
<p>Second paragraph with <code>inline code</code>.</p>
</article></body></html>
"""


class FakeHttp:
    """Small deterministic transport routed by URL substring."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> HttpResponse:
        self.calls.append(url)
        if "export.arxiv.org" in url and "id_list" in url:
            body, content_type = ARXIV_XML, "application/atom+xml"
        elif "export.arxiv.org" in url:
            body, content_type = ARXIV_XML, "application/atom+xml"
        elif "esearch.fcgi" in url:
            body, content_type = json.dumps({"esearchresult": {"idlist": ["12345678"]}}), "application/json"
        elif "efetch.fcgi" in url:
            body, content_type = PUBMED_XML, "text/xml"
        elif "api.crossref.org" in url:
            body, content_type = CROSSREF_JSON, "application/json"
        elif url == "https://example.com/post":
            body, content_type = ARTICLE_HTML, "text/html; charset=utf-8"
        else:
            return HttpResponse(
                status_code=404,
                url=url,
                headers={"content-type": "text/plain"},
                content=b"not found",
            )
        return HttpResponse(
            status_code=200,
            url=url,
            headers={"content-type": content_type},
            content=body.encode("utf-8"),
        )


@pytest.mark.asyncio
async def test_lifecycle_arxiv_feed_and_channels(tmp_path: Path) -> None:
    plugin = JournalPlugin(http_client=FakeHttp())
    ctx = PluginContext(
        "plugin-journals",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    await plugin.on_load(ctx)
    try:
        assert ctx.capabilities.has("text.source.v1")
        assert ctx.capabilities.has("text.citation.v1")
        result = await plugin.handle_text_source_rpc({"action": "channels"})
        assert result.ok
        channel_ids = [channel["id"] for channel in result.data["channels"]]
        assert channel_ids[:1] == ["arxiv"]
        assert "pubmed" in channel_ids

        feed = await plugin.handle_text_source_rpc(
            {"action": "fetch_feed", "channel_id": "arxiv", "query": "agents"}
        )
        assert feed.ok
        assert feed.data["count"] == 1
        item = feed.data["items"][0]
        assert item["id"] == "arxiv:2401.01234"
        assert item["title"] == "Reliable Agents for Scientific Reading"
        assert item["doi"] == "10.1000/example"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-journals")


@pytest.mark.asyncio
async def test_pubmed_feed_and_article(tmp_path: Path) -> None:
    plugin = JournalPlugin(http_client=FakeHttp())
    ctx = PluginContext(
        "plugin-journals",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    await plugin.on_load(ctx)
    try:
        feed = await plugin.handle_text_source_rpc(
            {"action": "fetch_feed", "channel_id": "pubmed", "query": "foundation model"}
        )
        assert feed.ok
        assert feed.data["count"] == 1
        item = feed.data["items"][0]
        assert item["id"] == "pubmed:12345678"
        assert item["title"] == "Foundation models for biology"
        assert item["author"] == "Wei Chen"

        article = await plugin.handle_text_source_rpc(
            {"action": "fetch_article", "article_id": "pubmed:12345678"}
        )
        assert article.ok
        paragraphs = article.data["article"]["paragraphs"]
        assert paragraphs[0]["original"].startswith("BACKGROUND:")
        assert article.data["article"]["journal_name"] == "Nature Machine Intelligence"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-journals")


@pytest.mark.asyncio
async def test_crossref_citation_lookup(tmp_path: Path) -> None:
    plugin = JournalPlugin(http_client=FakeHttp())
    ctx = PluginContext(
        "plugin-journals",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_citation_rpc(
            {"action": "lookup", "doi": "10.1038/s42256-026-00001"}
        )
        assert result.ok
        citation = result.data["citation"]
        assert citation["title"] == "A foundation model for biology"
        assert citation["authors"] == ["Wei Chen", "Alice Zhang"]
        assert citation["journal"] == "Nature Machine Intelligence"
        assert "https://doi.org/" in citation["plain"]
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-journals")


@pytest.mark.asyncio
async def test_web_article_extraction(tmp_path: Path) -> None:
    plugin = JournalPlugin(http_client=FakeHttp())
    ctx = PluginContext(
        "plugin-journals",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    await plugin.on_load(ctx)
    try:
        article = await plugin.fetch_article("https://example.com/post")
        assert article.title == "A Blog Post"
        assert article.cover_url == "https://example.com/cover.png"
        assert [paragraph.kind for paragraph in article.paragraphs[:3]] == [
            "heading",
            "paragraph",
            "heading",
        ]
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-journals")


def test_create_plugin_returns_manifest_entrypoint() -> None:
    assert isinstance(create_plugin(), JournalPlugin)

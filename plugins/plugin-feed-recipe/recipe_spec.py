"""Declarative Feed Recipe specification models, validation, and built-in starter recipes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

if __package__:
    from .security import FeedRecipeError, validate_https_url, validate_recipe_id
else:
    from security import FeedRecipeError, validate_https_url, validate_recipe_id

_VALID_FORMATS = frozenset({"json", "html", "xml", "lua"})
_VALID_METHODS = frozenset({"GET", "POST"})
_VALID_ARTICLE_MODES = frozenset({"readability", "json", "html", "chrome"})


@dataclass(slots=True)
class CommentSpec:
    """Declarative comment extraction and nesting rules."""

    url: str = ""
    path: str = ""
    item_selector: str = ""
    id: str = "id"
    parent_id: str = ""
    author: str = "author"
    role_badge: str = ""
    body: str = "body"
    created_at: str = "created_at"
    score: str = ""
    replies: str = ""
    indent_selector: str = ""
    indent_attr: str = ""
    indent_step: int = 0


@dataclass(slots=True)
class ArticleSpec:
    """Declarative article body and comment extraction rules."""

    url: str = "{{url}}"
    mode: str = "readability"
    body_selector: str = ""
    title_selector: str = ""
    author_selector: str = ""
    published_selector: str = ""
    content_path: str = ""
    follow_url_path: str = "url"
    tags: list[str] = field(default_factory=list)
    comments: CommentSpec = field(default_factory=CommentSpec)


@dataclass(slots=True)
class FeedSpec:
    """Declarative feed list request and field mapping."""

    url: str = ""
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    items_path: str = ""
    item_selector: str = ""
    max_items: int = 30
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RecipeSpec:
    """Validated feed + article + comment recipe descriptor."""

    id: str
    name: str
    category: str = "blog"
    format: str = "json"
    enabled: bool = True
    use_chrome_cookies: bool = False
    cookie_domain: str = ""
    cache_ttl_sec: int = -1
    auto_translate: bool = True
    tags: list[str] = field(default_factory=list)
    feed: FeedSpec = field(default_factory=FeedSpec)
    article: ArticleSpec = field(default_factory=ArticleSpec)
    source_path: str = ""
    raw_source: str = ""
    is_lua_script: bool = False
    is_builtin: bool = False
    has_lua_transform_item: bool = False
    has_lua_fetch_feed: bool = False
    has_lua_fetch_article: bool = False


@dataclass(frozen=True, slots=True)
class BundledRecipe:
    """Compiled-in starter recipe matching v1 builtinStarterRecipes()."""

    filename: str
    content: str


BUILTIN_STARTER_RECIPES: tuple[BundledRecipe, ...] = (
    BundledRecipe(
        "hackernews.json",
        """{
  "id": "hackernews",
  "name": "Hacker News",
  "category": "blog",
  "format": "json",
  "feed": {
    "url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30",
    "items_path": "hits",
    "fields": {
      "id": "objectID",
      "title": "title",
      "url": "url|https://news.ycombinator.com/item?id={{objectID}}",
      "author": "author",
      "score": "points",
      "comment_count": "num_comments",
      "published_at": "created_at"
    }
  },
  "article": {
    "url": "https://hn.algolia.com/api/v1/items/{{id}}",
    "mode": "readability",
    "comments": {
      "path": "children",
      "id": "id",
      "author": "author",
      "body": "text",
      "created_at": "created_at",
      "score": "points",
      "replies": "children"
    }
  }
}
""",
    ),
    BundledRecipe(
        "v2ex.json",
        """{
  "id": "v2ex",
  "name": "V2EX 技术",
  "category": "blog",
  "format": "html",
  "use_chrome_cookies": true,
  "feed": {
    "url": "https://www.v2ex.com/?tab=tech",
    "item_selector": "div.cell.item",
    "fields": {
      "id": "span.item_title a@href",
      "title": "span.item_title a",
      "url": "span.item_title a@href",
      "author": "span.topic_info strong:first-child a",
      "comment_count": "a.count_livid"
    }
  },
  "article": {
    "url": "{{url}}",
    "body_selector": ".topic_content, .markdown_body",
    "comments": {
      "item_selector": "#Main .box .cell[id^=r_]",
      "author": "strong a.dark",
      "body": ".reply_content",
      "score": ".small.fade",
      "created_at": "span.ago"
    }
  }
}
""",
    ),
    BundledRecipe(
        "scholar_blog.json",
        """{
  "id": "scholar_blog",
  "name": "学者个人博客 (RSS/Atom)",
  "category": "blog",
  "format": "xml",
  "feed": {
    "url": "https://lilianweng.github.io/index.xml",
    "max_items": 20
  },
  "article": {
    "url": "{{url}}",
    "mode": "readability",
    "body_selector": "article, .post-content, main",
    "tags": ["Blog", "ML", "Research"]
  }
}
""",
    ),
    BundledRecipe(
        "conference_papers.lua",
        """return {
  id = "conference_papers",
  name = "顶会与前沿论文 (cs.AI)",
  category = "journal",
  format = "xml",
  use_chrome_cookies = false,
  tags = { "AI", "Conference", "Papers" },
  feed = {
    url = "https://rss.arxiv.org/rss/cs.AI",
    max_items = 25,
  },
  article = {
    url = "{{url}}",
    mode = "readability",
    body_selector = "blockquote.abstract, .abstract, main",
  },
  transform_item = function(item)
    if item.title then item.title = item.title:gsub("^%s+", ""):gsub("%s+$", "") end
    return item
  end,
}
""",
    ),
)


def _str_val(obj: Mapping[str, Any], key: str, default: str = "") -> str:
    val = obj.get(key)
    return val.strip() if isinstance(val, str) else default


def _int_val(obj: Mapping[str, Any], key: str, default: int) -> int:
    val = obj.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        return default
    return val


def _bool_val(obj: Mapping[str, Any], key: str, default: bool) -> bool:
    val = obj.get(key)
    return val if isinstance(val, bool) else default


def parse_recipe_dict(
    data: Any,
    *,
    source_path: str = "",
    raw_source: str = "",
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> RecipeSpec:
    """Validate and parse a raw JSON/Lua table dict into a RecipeSpec."""
    if not isinstance(data, Mapping):
        raise FeedRecipeError("validation_error", "Recipe root must be a JSON object or Lua table")

    rid = validate_recipe_id(data.get("id"), field_name="id")
    name = _str_val(data, "name") or rid
    category = (_str_val(data, "category") or "blog").lower() or "blog"
    fmt = (_str_val(data, "format") or "json").lower()
    if fmt not in _VALID_FORMATS:
        raise FeedRecipeError("validation_error", f"Unsupported recipe format {fmt!r}")

    enabled = _bool_val(data, "enabled", True)
    use_chrome_cookies = _bool_val(data, "use_chrome_cookies", False)
    cookie_domain = _str_val(data, "cookie_domain").lower()
    cache_ttl_sec = _int_val(data, "cache_ttl_sec", -1)
    auto_translate = _bool_val(data, "auto_translate", True)

    tags: list[str] = []
    if isinstance(data.get("tags"), list):
        tags = [t.strip() for t in data["tags"] if isinstance(t, str) and t.strip()]

    has_fetch_feed = bool(data.get("_has_fetch_feed", False))
    has_fetch_article = bool(data.get("_has_fetch_article", False))
    has_transform_item = bool(data.get("_has_transform_item", False))

    feed_spec = FeedSpec()
    raw_feed = data.get("feed")
    if isinstance(raw_feed, Mapping):
        feed_url = _str_val(raw_feed, "url")
        method = (_str_val(raw_feed, "method") or "GET").upper()
        if method not in _VALID_METHODS:
            raise FeedRecipeError("validation_error", f"Unsupported feed.method {method!r}")
        feed_headers = {
            str(k): str(v)
            for k, v in (raw_feed.get("headers") or {}).items()
            if isinstance(raw_feed.get("headers"), Mapping) and isinstance(k, str) and isinstance(v, str)
        }
        feed_fields = {
            str(k): str(v)
            for k, v in (raw_feed.get("fields") or {}).items()
            if isinstance(raw_feed.get("fields"), Mapping) and isinstance(k, str) and isinstance(v, str)
        }
        max_items = _int_val(raw_feed, "max_items", 30)
        if max_items <= 0:
            max_items = 30
        feed_spec = FeedSpec(
            url=feed_url,
            method=method,
            headers=feed_headers,
            items_path=_str_val(raw_feed, "items_path"),
            item_selector=_str_val(raw_feed, "item_selector"),
            max_items=min(200, max_items),
            fields=feed_fields,
        )
    elif not has_fetch_feed:
        raise FeedRecipeError("validation_error", "Recipe missing 'feed' object (or Lua fetch_feed function)")

    if not has_fetch_feed:
        # Strip {{query}} / {{page}} placeholders solely for scheme/host validation
        url_for_check = feed_spec.url.replace("{{query|url}}", "q").replace("{{query}}", "q").replace("{{page}}", "1")
        validate_https_url(
            url_for_check,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
            field_name="feed.url",
        )

    article_spec = ArticleSpec()
    raw_art = data.get("article")
    if isinstance(raw_art, Mapping):
        mode = (_str_val(raw_art, "mode") or "readability").lower()
        if mode not in _VALID_ARTICLE_MODES:
            mode = "readability"
        art_tags = (
            [t.strip() for t in raw_art["tags"] if isinstance(t, str) and t.strip()]
            if isinstance(raw_art.get("tags"), list)
            else []
        )
        cs = CommentSpec()
        raw_cm = raw_art.get("comments")
        if isinstance(raw_cm, Mapping):
            cs = CommentSpec(
                url=_str_val(raw_cm, "url"),
                path=_str_val(raw_cm, "path"),
                item_selector=_str_val(raw_cm, "item_selector"),
                id=_str_val(raw_cm, "id") or "id",
                parent_id=_str_val(raw_cm, "parent_id"),
                author=_str_val(raw_cm, "author") or "author",
                role_badge=_str_val(raw_cm, "role_badge"),
                body=_str_val(raw_cm, "body") or "body",
                created_at=_str_val(raw_cm, "created_at") or "created_at",
                score=_str_val(raw_cm, "score"),
                replies=_str_val(raw_cm, "replies"),
                indent_selector=_str_val(raw_cm, "indent_selector"),
                indent_attr=_str_val(raw_cm, "indent_attr"),
                indent_step=max(0, _int_val(raw_cm, "indent_step", 0)),
            )
        article_spec = ArticleSpec(
            url=_str_val(raw_art, "url") or "{{url}}",
            mode=mode,
            body_selector=_str_val(raw_art, "body_selector"),
            title_selector=_str_val(raw_art, "title_selector"),
            author_selector=_str_val(raw_art, "author_selector"),
            published_selector=_str_val(raw_art, "published_selector"),
            content_path=_str_val(raw_art, "content_path"),
            follow_url_path=_str_val(raw_art, "follow_url_path") or "url",
            tags=art_tags,
            comments=cs,
        )

    return RecipeSpec(
        id=rid,
        name=name,
        category=category,
        format=fmt,
        enabled=enabled,
        use_chrome_cookies=use_chrome_cookies,
        cookie_domain=cookie_domain,
        cache_ttl_sec=cache_ttl_sec,
        auto_translate=auto_translate,
        tags=tags,
        feed=feed_spec,
        article=article_spec,
        source_path=source_path,
        raw_source=raw_source,
        is_lua_script=False,
        is_builtin=False,
        has_lua_transform_item=has_transform_item,
        has_lua_fetch_feed=has_fetch_feed,
        has_lua_fetch_article=has_fetch_article,
    )


def recipe_to_dict(spec: RecipeSpec) -> dict[str, Any]:
    """Serialize RecipeSpec matching v1 recipeToJson()."""
    cs = spec.article.comments
    return {
        "id": spec.id,
        "name": spec.name,
        "category": spec.category,
        "format": spec.format,
        "enabled": spec.enabled,
        "use_chrome_cookies": spec.use_chrome_cookies,
        "cookie_domain": spec.cookie_domain,
        "cache_ttl_sec": spec.cache_ttl_sec,
        "auto_translate": spec.auto_translate,
        "tags": list(spec.tags),
        "is_lua_script": spec.is_lua_script,
        "is_builtin": spec.is_builtin,
        "source_path": spec.source_path,
        "feed": {
            "url": spec.feed.url,
            "method": spec.feed.method,
            "headers": dict(spec.feed.headers),
            "items_path": spec.feed.items_path,
            "item_selector": spec.feed.item_selector,
            "max_items": spec.feed.max_items,
            "fields": dict(spec.feed.fields),
        },
        "article": {
            "url": spec.article.url,
            "mode": spec.article.mode,
            "body_selector": spec.article.body_selector,
            "title_selector": spec.article.title_selector,
            "author_selector": spec.article.author_selector,
            "published_selector": spec.article.published_selector,
            "content_path": spec.article.content_path,
            "follow_url_path": spec.article.follow_url_path,
            "tags": list(spec.article.tags),
            "comments": {
                "url": cs.url,
                "path": cs.path,
                "item_selector": cs.item_selector,
                "id": cs.id,
                "parent_id": cs.parent_id,
                "author": cs.author,
                "role_badge": cs.role_badge,
                "body": cs.body,
                "created_at": cs.created_at,
                "score": cs.score,
                "replies": cs.replies,
                "indent_selector": cs.indent_selector,
                "indent_attr": cs.indent_attr,
                "indent_step": cs.indent_step,
            },
        },
    }


__all__ = [
    "ArticleSpec",
    "BUILTIN_STARTER_RECIPES",
    "BundledRecipe",
    "CommentSpec",
    "FeedSpec",
    "RecipeSpec",
    "parse_recipe_dict",
    "recipe_to_dict",
]

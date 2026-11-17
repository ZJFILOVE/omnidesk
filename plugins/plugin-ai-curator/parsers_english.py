"""Fail-closed parsers for English Daily, Translation, Vocabulary, Weekly, and Weekly/TV."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk import BookPageBlock, TextArticle, TextFeedItem, TextParagraph, VocabCard

if __package__:
    from .models import EnglishNoteEntry, read_text_strict, truncate_text
    from .parsers_common import (
        extract_user_notes_after_auto_end,
        match_bold_field,
        parse_frontmatter,
        parse_numbered_heading,
        strip_auto_comments,
    )
else:
    from models import EnglishNoteEntry, read_text_strict, truncate_text  # type: ignore[no-redef]
    from parsers_common import (  # type: ignore[no-redef]
        extract_user_notes_after_auto_end,
        match_bold_field,
        parse_frontmatter,
        parse_numbered_heading,
        strip_auto_comments,
    )

_SOURCE_ID = "plugin-ai-curator"
_IPA_TAIL_RE = re.compile(r"\s+(/[^/\n]+/)\s*$")
_MD_STRIP_RE = re.compile(r"[#>*_`]+")

ENGLISH_SUBDIRS: tuple[tuple[str, str, str], ...] = (
    ("Daily", "daily", "English Daily"),
    ("Translation", "translation", "English Translation"),
    ("Vocabulary", "vocabulary", "English Vocabulary"),
    ("Weekly", "weekly", "English Weekly"),
    ("Weekly/TV", "weekly-tv", "English Weekly TV"),
)


def parse_vocabulary_markdown(md_text: str, date: str) -> list[VocabCard]:
    """Parse `50-courses/English/Vocabulary/YYYY-MM-DD.md` into SM-2 VocabCard list."""
    _, body = parse_frontmatter(md_text)
    lines = strip_auto_comments(body).splitlines()

    cards: list[VocabCard] = []
    cur: dict[str, Any] | None = None
    sub = ""

    def flush() -> None:
        nonlocal cur, sub
        if cur is None or not cur["word"].strip():
            cur = None
            sub = ""
            return
        word = cur["word"].strip()
        etym_parts = list(cur["etym_lines"])
        if cur["extra_notes"]:
            etym_parts.extend(cur["extra_notes"])
        cards.append(
            VocabCard(
                id=f"vocab-{date}-{cur['rank']}",
                word=word,
                lemma=word.lower(),
                phonetic=cur["phonetic"],
                etymology="\n".join(etym_parts).strip(),
                meaning=cur["meaning"].strip(),
                context_sentence=" ".join(cur["example_lines"]).strip(),
                source_book=f"English Vocabulary · {date}",
                source_date=date,
                interval_days=1,
                repetitions=0,
                ease_factor=2.5,
            )
        )
        cur = None
        sub = ""

    for raw_line in lines:
        t = raw_line.strip()
        numbered = parse_numbered_heading(t)
        if numbered is not None:
            flush()
            rank, wtitle = numbered
            phonetic = ""
            m_ipa = _IPA_TAIL_RE.search(wtitle)
            if m_ipa:
                phonetic = m_ipa.group(1)
                word = wtitle[: m_ipa.start()].strip()
            else:
                word = wtitle.strip()
            cur = {
                "rank": rank,
                "word": word,
                "phonetic": phonetic,
                "meaning": "",
                "etym_lines": [],
                "extra_notes": [],
                "example_lines": [],
            }
            continue
        if t.startswith("## "):
            flush()
            continue
        if cur is None or not t or t == "---":
            continue
        if t.startswith("### "):
            sh = t[4:].strip().lower()
            if "origin" in sh or "memory" in sh:
                sub = "origin"
            elif "family" in sh:
                sub = "family"
            elif "collocation" in sh or "use" in sh:
                sub = "collocations"
            elif "example" in sh:
                sub = "example"
            else:
                sub = "other"
            continue

        if (val := match_bold_field(t, ("含义", "Meaning"))) is not None:
            cur["meaning"] = val
        elif (val := match_bold_field(t, ("常见搭配", "Collocations"))) is not None:
            cur["extra_notes"].append(f"搭配: {val}")
        elif (val := match_bold_field(t, ("例句", "Example"))) is not None:
            cur["example_lines"].append(val.strip("*"))
        elif (val := match_bold_field(t, ("词族", "词形"))) is not None:
            cur["etym_lines"].append(f"词族: {val}")
        elif (val := match_bold_field(t, ("对比", "辨析"))) is not None:
            cur["extra_notes"].append(f"辨析: {val}")
        elif not sub and t.startswith("**") and t.endswith("**") and len(t) > 4 and not cur["meaning"]:
            cur["meaning"] = t[2:-2].strip()
        elif sub == "origin":
            cur["etym_lines"].append(t)
        elif sub in {"family", "collocations"}:
            cur["extra_notes"].append(t)
        elif sub == "example":
            ex = t[1:].strip() if t.startswith(">") else t
            cur["example_lines"].append(ex)

    flush()
    return cards


def parse_translation_blocks(md_body: str) -> list[BookPageBlock]:
    """Parse bilingual translation exercises (`> [!answer]`) into aligned BookPageBlock list."""
    blocks: list[BookPageBlock] = []
    cur_title = ""
    zh_lines: list[str] = []
    en_lines: list[str] = []
    in_answer = False
    bidx = 1

    def flush() -> None:
        nonlocal cur_title, zh_lines, en_lines, in_answer, bidx
        zh = "\n".join(zh_lines).strip()
        en = "\n".join(en_lines).strip()
        if not zh and not en:
            return
        if cur_title:
            blocks.append(BookPageBlock(id=f"b-{bidx}", kind="heading", original=cur_title))
            bidx += 1
        blocks.append(
            BookPageBlock(
                id=f"b-{bidx}",
                kind="paragraph",
                original=en or zh,
                translated=zh if en else "",
            )
        )
        bidx += 1
        cur_title = ""
        zh_lines = []
        en_lines = []
        in_answer = False

    for raw_line in strip_auto_comments(md_body).splitlines():
        t = raw_line.strip()
        if t.startswith(("## 练习", "## Exercise", "## 间隔复习")) or re.match(r"^##\s+\d+\.\s+", t):
            flush()
            cur_title = t[3:].strip()
            continue
        if t.startswith("> [!answer]"):
            in_answer = True
            continue
        if in_answer:
            if t.startswith(">"):
                ans = t[1:].strip()
                if ans:
                    en_lines.append(ans)
            elif not t or t.startswith("#"):
                in_answer = False
            continue
        if cur_title and t and not t.startswith(("### ", "**建议时间")):
            zh_lines.append(t)

    flush()
    return blocks


def parse_markdown_as_blocks(
    md_body: str,
    vocab_words: Sequence[str] = (),
    skip_h1_title: str = "",
) -> list[BookPageBlock]:
    """Convert Markdown body into structured BookPageBlock items with highlighted vocab."""
    blocks: list[BookPageBlock] = []
    cur_lines: list[str] = []
    bidx = 1
    skipped_h1 = False
    in_fence = False
    fence_char = ""
    fence_len = 0

    def flush_para() -> None:
        nonlocal bidx
        text = "\n".join(cur_lines).strip()
        cur_lines.clear()
        if not text:
            return
        lower_text = text.lower()
        hits = [w for w in vocab_words if w and w.lower() in lower_text]
        blocks.append(BookPageBlock(id=f"b-{bidx}", kind="paragraph", original=text, highlighted_words=hits))
        bidx += 1

    for raw_line in strip_auto_comments(md_body).splitlines():
        t = raw_line.strip()
        if not in_fence and (t.startswith("```") or t.startswith("~~~")):
            in_fence = True
            fence_char = t[0]
            fence_len = len(t) - len(t.lstrip(fence_char))
            cur_lines.append(raw_line.rstrip())
            continue
        if in_fence:
            cur_lines.append(raw_line.rstrip())
            stripped = t.lstrip(fence_char)
            if len(t) - len(stripped) >= fence_len and not stripped.strip():
                in_fence = False
                flush_para()
            continue
        if not t:
            flush_para()
            continue
        if t.startswith("# "):
            flush_para()
            h1 = t[2:].strip()
            if not skipped_h1 and skip_h1_title and h1 == skip_h1_title:
                skipped_h1 = True
                continue
            blocks.append(BookPageBlock(id=f"b-{bidx}", kind="heading", original=h1))
            bidx += 1
        elif t.startswith(("## ", "### ")):
            flush_para()
            heading = t[4:].strip() if t.startswith("### ") else t[3:].strip()
            blocks.append(BookPageBlock(id=f"b-{bidx}", kind="heading", original=heading))
            bidx += 1
        else:
            cur_lines.append(raw_line.rstrip())

    flush_para()
    return blocks


def _markdown_excerpt(md_body: str, max_chars: int = 180) -> str:
    clean = _MD_STRIP_RE.sub(" ", strip_auto_comments(md_body))
    collapsed = " ".join(clean.split())
    return truncate_text(collapsed, max_chars)


def parse_english_file(
    md_path: Path,
    repo_root: Path,
    *,
    sub_rel: str,
    component: str,
    label: str,
) -> dict[str, Any]:
    """Parse one English Daily/Translation/Vocabulary/Weekly/Weekly-TV markdown file."""
    date = md_path.stem
    raw = read_text_strict(md_path, repo_root=repo_root)
    fm, body = parse_frontmatter(raw)
    user_notes = extract_user_notes_after_auto_end(raw)
    clean_body = strip_auto_comments(body)

    title = ""
    for line in clean_body.splitlines():
        t = line.strip()
        if t.startswith("# "):
            title = t[2:].strip()
            break
    if not title:
        title = fm.get("source_title") or fm.get("title") or f"{label} · {date}"

    day_vocabs = parse_vocabulary_markdown(raw, date) if component == "vocabulary" else []
    vocab_words = [vc.word for vc in day_vocabs]
    full_blocks = parse_markdown_as_blocks(clean_body, vocab_words, skip_h1_title=title)
    book_blocks = parse_translation_blocks(clean_body) if component == "translation" else []
    if not book_blocks:
        book_blocks = full_blocks

    rel_path = f"50-courses/English/{sub_rel}/{date}.md"
    note = EnglishNoteEntry(
        component=component,
        date=date,
        title=title,
        rel_path=rel_path,
        blocks=book_blocks,
        vocab_cards=day_vocabs,
    )

    eid = f"english-{component}-{date}"
    if fm.get("source_title"):
        eng_excerpt = fm["source_title"] + (f" · {fm['level']}" if fm.get("level") else "")
    else:
        eng_excerpt = _markdown_excerpt(clean_body, 180)
    author = fm.get("source") or "AI Curation"
    url = fm.get("source_url") or ""
    tags = ["english", component, date]
    bilingual_cached = component in {"translation", "vocabulary"}

    feed_item = TextFeedItem(
        id=eid,
        source_id=_SOURCE_ID,
        source_name=label,
        category="english",
        channel_id="english",
        title=title,
        excerpt=truncate_text(eng_excerpt, 220),
        author=author,
        published_at=date,
        url=url,
        tags=tags,
        bilingual_cached=bilingual_cached,
        curation_mode="daily_immutable",
        business_date=date,
        extra={
            "curation_mode": "daily_immutable",
            "business_date": date,
            "component": component,
            "rel_path": rel_path,
            "user_notes": user_notes,
        },
    )
    article = TextArticle(
        id=eid,
        source_id=_SOURCE_ID,
        source_name=label,
        category="english",
        channel_id="english",
        title=title,
        subtitle=f"{label} · {date}" + (f" · {fm['level']}" if fm.get("level") else ""),
        authors=author,
        published_at=date,
        url=url,
        tags=tags,
        paragraphs=[
            TextParagraph(
                id=f"p-{idx}",
                kind=blk.kind if blk.kind in {"heading", "paragraph", "image"} else "paragraph",
                original=blk.original,
                translated=blk.translated,
            )
            for idx, blk in enumerate(full_blocks, start=1)
        ],
        curation_mode="daily_immutable",
        business_date=date,
        extra={"component": component, "rel_path": rel_path, "user_notes": user_notes},
    )
    return {
        "date": date,
        "component": component,
        "note": note,
        "feed_item": feed_item,
        "article": article,
        "vocab_cards": day_vocabs,
    }

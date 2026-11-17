# plugin-ebook

OmniDesk v2 `hybrid` plugin (`domain = "book"`) providing `book.source.v1` and `vocab.sm2.v1`.

## Features
- Multi-root local library scanning (`library_roots`) for `.epub`, `.txt`, `.mobi`, and `.azw3`
- Stdlib `zipfile` + `xml` EPUB parser (`container.xml` / OPF / spine / NAV / NCX) with image placeholders and paragraph-level pagination
- Strict UTF-8 / GB18030 detection and paragraph pagination for `.txt`
- Configurable `calibre_bin` (`ebook-convert`) subprocess conversion for `.mobi` / `.azw3` (fails closed with `format_unavailable` when absent)
- SM-2 spaced-repetition vocabulary store (`add`, `list`, `review`, `due`, `import`, `export`) with case normalization, idempotent import, and `0600` atomic persistence at `$XDG_DATA_HOME/omnidesk/vocab/plugin-ebook.json`
- Companion actions (`lookup_term`, `explain_sentence`) with offline fallback (`text_only=true`) and optional `llm.chat.v1`, `media.clip_jump.v1`, and `book.sync.v1` integration

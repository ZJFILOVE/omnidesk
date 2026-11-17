"""Shared offline git repo and archive fixtures for plugin-ai-curator tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = Path(__file__).resolve().parents[1]

for _p in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def run_git(args: list[str], cwd: Path) -> str:
    """Run a local offline git command in test setup."""
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def init_git_repo(repo_dir: Path, *, branch: str = "main") -> None:
    """Initialize a local git repo with test author identity."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-b", branch], repo_dir)
    run_git(["config", "user.name", "OmniDesk Test"], repo_dir)
    run_git(["config", "user.email", "test@omnidesk.local"], repo_dir)


def commit_all(repo_dir: Path, message: str) -> str:
    """Stage all changes and commit in local test git repo."""
    run_git(["add", "-A"], repo_dir)
    run_git(["commit", "-m", message], repo_dir)
    return run_git(["rev-parse", "HEAD"], repo_dir)


def create_bare_pair(base_dir: Path, repo_name: str) -> tuple[Path, Path]:
    """Create a bare remote and a seeded working checkout on branch `main`."""
    bare = base_dir / f"{repo_name}.git"
    bare.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--bare", "-b", "main"], bare)
    seed = base_dir / f"{repo_name}-seed"
    init_git_repo(seed, branch="main")
    (seed / "README.md").write_text(f"# {repo_name}\n", encoding="utf-8")
    commit_all(seed, "initial commit")
    run_git(["remote", "add", "origin", str(bare)], seed)
    run_git(["push", "-u", "origin", "main"], seed)
    return bare, seed


def _make_track(pos: int, title: str, artist: str, album: str, song_id: str) -> dict[str, Any]:
    return {
        "position": pos,
        "title": title,
        "artist": artist,
        "album": album,
        "canonical_id": f"{artist}::{title}",
        "apple_music": {
            "song_id": song_id,
            "url": f"https://music.apple.com/cn/song/{song_id}",
            "storefront": "cn",
        },
        "analysis_summary": f"深度解析 {title} 的声部与编曲留白。",
        "listening_cue": f"第一遍听 {artist} 句尾呼吸，第二遍听低频律动。",
    }


def populate_full_archive_fixtures(obsidian_dir: Path, music_dir: Path) -> None:
    """Write all 6 stream fixtures, 2026-11-06 4-track anomaly, pre-cutoff file, and ledgers."""
    # Pre-2026-09-04 entry should be ignored by rehydrator.
    old_music = music_dir / "recommendations" / "2026" / "09" / "2026-09-03.json"
    old_music.parent.mkdir(parents=True, exist_ok=True)
    old_music.write_text(
        json.dumps({"schema_version": 1, "date": "2026-09-03", "tracks": [_make_track(1, "Old Song", "Old Artist", "Old Album", "900001")]}),
        encoding="utf-8",
    )

    # Normal 5-track day: 2026-11-13
    m13 = music_dir / "recommendations" / "2026" / "11" / "2026-11-13.json"
    m13.parent.mkdir(parents=True, exist_ok=True)
    m13.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "apple_music",
                "date": "2026-11-13",
                "tracks": [
                    _make_track(1, "Muerte", "Natalia Lafourcade", "De Todas las Flores", "1643009587"),
                    _make_track(2, "Plastic 100°C", "Sampha", "Process", "1607100336"),
                    _make_track(3, "Tobiume (2025 Remaster)", "Susumu Yokota", "Sakura", "1833283533"),
                    _make_track(4, "O Leaozinho", "Caetano Veloso", "Bicho", "3735675"),
                    _make_track(5, "Trouble of the World", "Mahalia Jackson", "Gospel", "1032611724"),
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Known 4-track anomaly day: 2026-11-06 (positions 1, 2, 4, 5 — missing 3)
    m06 = music_dir / "recommendations" / "2026" / "11" / "2026-11-06.json"
    m06.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "apple_music",
                "date": "2026-11-06",
                "tracks": [
                    _make_track(1, "丽水街的丽水海边", "许含光", "太平洋大酒店", "1847866026"),
                    _make_track(2, "Early Summer", "Ryo Fukui", "Scenery", "1329457872"),
                    _make_track(4, "La chanson", "Claude Nougaro", "Chansons", "1443122222"),
                    _make_track(5, "Fado Final", "Amália Rodrigues", "Fado", "1443123333"),
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state_tracks = music_dir / "state" / "tracks.json"
    state_tracks.parent.mkdir(parents=True, exist_ok=True)
    state_tracks.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "apple_music",
                "recommended": {"Natalia Lafourcade::Muerte": 1, "许含光::丽水街的丽水海边": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Obsidian mirror Music manifests & handwritten ## Notes
    obs_man = obsidian_dir / "Music" / "Daily" / "manifests" / "2026-11-06.json"
    obs_man.parent.mkdir(parents=True, exist_ok=True)
    obs_man.write_text(m06.read_text(encoding="utf-8"), encoding="utf-8")

    obs_md_13 = obsidian_dir / "Music" / "Daily" / "2026-11-13.md"
    obs_md_13.parent.mkdir(parents=True, exist_ok=True)
    obs_md_13.write_text(
        "---\ndate: 2026-11-13\ntype: music-daily\n---\n<!-- AUTO:START -->\n# Apple Music Daily 5 · 2026-11-13\n<!-- AUTO:END -->\n\n## Notes\n深夜耳机试听第 1 首吉他的空气感非常出色。\n",
        encoding="utf-8",
    )

    # YouTube Daily
    yt_13 = obsidian_dir / "Resources" / "YouTube" / "Daily" / "2026-11-13.md"
    yt_13.parent.mkdir(parents=True, exist_ok=True)
    yt_13.write_text(
        "---\ndate: 2026-11-13\ntype: youtube-daily\n---\n<!-- AUTO:START -->\n"
        "# YouTube Daily｜2026-11-13\n\n## 今日优先级\n先看第 1 条，时间有限看第 2 条。\n"
        "> 依据发布方官网嵌入核验。\n\n"
        "## 1. Incorruptible by Design\n- **频道**：Long Now\n- **主题**：组织治理\n"
        "- **链接**：https://www.youtube.com/watch?v=WrGRomn5pEQ\n"
        "### Why watch\n从治理结构看使命偏移。\n### 核验说明\nverification_method: creator_official_page\n\n"
        "## 2. William Wegman Early Video\n- **频道**：Met Museum\n- **主题**：录像艺术 · wildcard\n"
        "- **链接**：https://youtu.be/1ZUIrDTN6wo\n"
        "### Why watch\n有限取景中的幽默反转。\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )

    # News Daily
    news_13 = obsidian_dir / "News" / "Daily" / "2026-11-13.md"
    news_13.parent.mkdir(parents=True, exist_ok=True)
    news_13.write_text(
        "---\ndate: 2026-11-13\ntype: daily-news-daily\n---\n<!-- AUTO:START -->\n"
        "## Executive View\n甲烷倡议与纺织统计口径分别对应政策与统计证据。\n\n"
        "## 1. COP31 甲烷减排讨论\n- **事件日期**：2026-11-12\n- **影响等级**：4/5\n"
        "### 事实摘要\n强调填埋场甲烷监测资金。\n### Why it matters\nMRV 能力决定减排有效性。\n"
        "### Long-term impact\n推动独立核查机制落地。\n### What to watch next\n关注会议融资条款。\n"
        "### Sources\n- [UNFCCC](https://unfccc.int/news/methane-2026)\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )

    # Reddit Daily
    red_13 = obsidian_dir / "Resources" / "Reddit" / "Daily" / "2026-11-13.md"
    red_13.parent.mkdir(parents=True, exist_ok=True)
    red_13.write_text(
        "---\ndate: 2026-11-13\ntype: reddit-daily\n---\n<!-- AUTO:START -->\n"
        "> 未打开原始帖子，约龄为搜索显示。\n\n"
        "## 1. Thermocouples Calibration\n"
        "- **subreddit**：r/instrumentation\n- **主题**：热电偶校准\n"
        "- **链接**：https://www.reddit.com/r/instrumentation/comments/1n0o4w0\n"
        "- **日期/约龄**：1.2 years ago\n- **original_post_verification**：`not_performed_user_waived`\n"
        "### Why read\n区分传感器与整条测量链校准边界。\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )

    # Journal Daily + Index
    j_dir = obsidian_dir / "00-inbox" / "Journal"
    j_dir.mkdir(parents=True, exist_ok=True)
    (j_dir / "Index.md").write_text("---\ntype: journal-index\n---\n# Journal 索引\n", encoding="utf-8")
    (j_dir / "2026-11-16.md").write_text(
        "---\ndate: 2026-11-16\ntype: journal-daily\n---\n<!-- AUTO:START -->\n"
        "## 今日科学问题\n蛋白质如何把电场写入活性位点？\n**今日最值得精读：①。**\n\n"
        "## ① Extreme electric fields power catalysis in ketosteroid isomerase\n"
        "极强电场驱动 KSI 催化。Stephen D. Fried et al.，2014，*Science*。"
        "[DOI](https://doi.org/10.1126/science.1259802) · "
        "[PubMed](https://pubmed.ncbi.nlm.nih.gov/25525245/) · "
        "[PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC4668018/)\n\n"
        "- **问题与观测：** 测量活性位点 Stark 频移达 -144 MV/cm。\n"
        "- **作者解释：** 静电预组织降低反应势垒。\n"
        "- **证据边界与 reviewer_synthesis：** 19NT 为过渡态类似物，不能单独排除几何协同。\n"
        "- **追问：** 能否用非天然氨基酸做微扰？\n"
        "- **阅读范围与待核：** 已核读 PubMed 摘要与正文图表，未读 SI。\n"
        "## 建议阅读顺序\n先读①建立物理图像。\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )

    # English Daily, Translation, Vocabulary, Weekly, Weekly/TV
    eng = obsidian_dir / "50-courses" / "English"
    for sub in ("Daily", "Translation", "Vocabulary", "Weekly", "Weekly/TV"):
        (eng / sub).mkdir(parents=True, exist_ok=True)
    (eng / "Daily" / "2026-11-16.md").write_text(
        "---\ntype: english\ncomponent: daily\ndate: 2026-11-16\nsource: NOAA\nsource_title: Climate vs Weather\nsource_url: https://www.noaa.gov/explainers/climate-weather\nlevel: CET-6\n---\n"
        "<!-- AUTO:START -->\n# English Daily — 2026-11-16\n\nWeather is short-term; climate is long-term.\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )
    (eng / "Translation" / "2026-11-16.md").write_text(
        "---\ntype: english\ncomponent: translation\ndate: 2026-11-16\n---\n"
        "<!-- AUTO:START -->\n# English Translation — 2026-11-16\n\n"
        "## 练习一｜CET-6\n社区计划设置图书漂流柜。\n\n> [!answer]- 参考译文\n> A neighborhood plans to install book-sharing cabinets.\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )
    (eng / "Vocabulary" / "2026-11-16.md").write_text(
        "---\ntype: english\ncomponent: vocabulary\ndate: 2026-11-16\n---\n"
        "<!-- AUTO:START -->\n# English Vocabulary — 2026-11-16\n\n"
        "## 1. procrastinate /prəˈkræstəneɪt/\n"
        "- **含义：** v. 拖延\n- **例句：** I kept procrastinating on the report.\n"
        "### Word origin\npro- + crastinus.\n\n"
        "## 2. over the short / long term\n"
        "- **含义：** 从短期/长期来看\n- **例句：** *Over the long term, consistent observations matter.*\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )
    (eng / "Weekly" / "2026-11-15.md").write_text(
        "---\ntype: english-weekly\ncomponent: weekly-long\ndate: 2026-11-15\n---\n"
        "<!-- AUTO:START -->\n# Weekly Long English — 2026-11-15\nLong listening practice.\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )
    (eng / "Weekly" / "TV" / "2026-11-14.md").write_text(
        "---\ntype: english\ncomponent: weekly-tv\ndate: 2026-11-14\ntitle: Poker Face\n---\n"
        "<!-- AUTO:START -->\n# Weekly American TV — Poker Face\nRegional speech stretches.\n<!-- AUTO:END -->\n",
        encoding="utf-8",
    )

    # .recommendation ledgers & history
    rec = obsidian_dir / ".recommendation"
    (rec / "history").mkdir(parents=True, exist_ok=True)
    (rec / "transactions").mkdir(parents=True, exist_ok=True)
    (rec / "policy.json").write_text(json.dumps({"version": 2, "strict_from": "2026-09-11"}), encoding="utf-8")
    (rec / "history" / "youtube.jsonl").write_text(
        json.dumps({"date": "2026-11-13", "video_id": "WrGRomn5pEQ", "title": "Incorruptible by Design"}) + "\n",
        encoding="utf-8",
    )
    (rec / "history" / "reddit.jsonl").write_text(
        json.dumps({"date": "2026-11-13", "post_id": "1n0o4w0", "subreddit": "r/instrumentation"}) + "\n",
        encoding="utf-8",
    )
    (rec / "history" / "journal.jsonl").write_text(
        json.dumps(
            {
                "date": "2026-11-16",
                "doi": "10.1126/science.1259802",
                "normalized_doi": "10.1126/science.1259802",
                "title": "Extreme electric fields power catalysis in ketosteroid isomerase",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (rec / "history" / "english.jsonl").write_text(
        json.dumps({"date": "2026-11-16", "source_url": "https://www.noaa.gov/explainers/climate-weather", "title": "Climate vs Weather"}) + "\n",
        encoding="utf-8",
    )

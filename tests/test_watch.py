"""Тесты режима --watch: без сети и без платных вызовов (run() подменён)."""

import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import channel_watch   # noqa: E402
import coinplay_sets   # noqa: E402
import generate        # noqa: E402
import state            # noqa: E402


# ------------------------------------------------------------ фикстуры/хелперы --

PRED_TEXT = """🤖 COINPLAY AI PREDICTIONS

{home} vs {away}
FA Cup · Football

🎯 5 of 10 models pick {home}
Most common score among those picks: 2-1
Average model confidence: {home} 38% · {away} 50% · Draw 12%
Coinplay AI, combined model estimate: {away} 53%

🔮 Confidence: Low
⚠️ Risk: Medium-High

🎮 Play on Coinplay

18+ · analysis & entertainment predictions · play responsibly"""

BET_BUILDER = """🤖 COINPLAY AI BET BUILDER

Scarborough Athletic vs Macclesfield FC
FA Cup · Football

⚡ Builder, 1 leg:
🔹 Macclesfield FC win ≈2.65x"""

POLL = "Your pick? Scarborough Athletic vs Macclesfield FC\nFA Cup · Football"


def _html_post(chan, mid, text):
    """Разметка как у t.me/s/<канал>: <br/>, эмодзи в <i><b>, сущности HTML."""
    body = (text.replace("&", "&amp;").replace("\n", "<br/>")
            .replace("🤖", '<i class="emoji" style="background-image:url(//x/1.png)"><b>🤖</b></i>'))
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
            f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" '
            f'data-post="{chan}/{mid}" data-view="x">'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{body}</div>'
            f'<a class="tgme_widget_message_date" href="https://t.me/{chan}/{mid}">'
            f'<time datetime="2026-10-06T14:31:00+00:00" class="time">16:31</time></a>'
            f'</div></div>')


def _page(chan, posts):
    return "<html><body>" + "".join(_html_post(chan, i, t) for i, t in posts) + "</body></html>"


def _pred(i):
    return (i, PRED_TEXT.format(home=f"Home{i}", away=f"Away{i}"))


@pytest.fixture
def rendered(monkeypatch):
    calls = []
    monkeypatch.setattr(generate, "run", lambda mt, args: calls.append(mt["home"]) or "x.zip")
    return calls


def _run_channel(monkeypatch, pages, chan="coinplayfootballai"):
    """Каждый цикл отдаёт очередную страницу превью канала."""
    it = iter(pages)
    monkeypatch.setattr(channel_watch, "fetch_html", lambda c: next(it))
    generate.watch(argparse.Namespace(), sleep=lambda s: None, max_cycles=len(pages))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WATCH_VERTICALS", "football")
    monkeypatch.setenv("WATCH_POLL_SEC", "5")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("SETS_API_TOKEN", raising=False)
    monkeypatch.delenv("WATCH_SOURCE", raising=False)
    monkeypatch.delenv("WATCH_CHANNELS", raising=False)


# ---------------------------------------------------------- парсер превью канала --

def test_parse_posts_and_filter_only_predictions():
    page = _page("coinplayfootballai", [(141, PRED_TEXT.format(home="Scarborough Athletic", away="Macclesfield FC")),
                                        (142, BET_BUILDER), (143, POLL)])
    posts = channel_watch.parse_posts(page)
    assert [p["id"] for p in posts] == [141, 142, 143]
    assert posts[0]["text"].startswith("🤖 COINPLAY AI PREDICTIONS")  # эмодзи без тегов
    assert [channel_watch.is_prediction(p["text"]) for p in posts] == [True, False, False]


def test_items_builds_match_with_clean_competition():
    page = _page("coinplayfootballai", [_pred(7), (8, BET_BUILDER)])
    its = channel_watch.items({"football": "coinplayfootballai"}, fetch=lambda c: page)
    assert len(its) == 1  # Bet Builder не считается предиктом
    m = its[0]["match"]
    assert its[0]["key"] == "coinplay-caption-coinplayfootballai-7"
    assert (m["home"], m["away"]) == ("Home7", "Away7")
    assert m["competition"] == "FA Cup"          # "· Football" срезан
    assert (m["rows"][0]["home"], m["rows"][0]["away"]) == (2, 1)
    assert m["consensus"] == "HOME7"


def test_items_unparseable_prediction_is_none_not_crash():
    broken = "🤖 COINPLAY AI PREDICTIONS\n\nтут нет матча"
    page = _page("coinplayfootballai", [(1, broken), _pred(2)])
    its = channel_watch.items({"football": "coinplayfootballai"}, fetch=lambda c: page)
    assert [i["match"] is None for i in its] == [True, False]


def test_all_channels_down_raises():
    def down(c):
        raise channel_watch.ChannelError("403")
    with pytest.raises(channel_watch.ChannelError):
        channel_watch.items({"football": "x"}, fetch=down)


def test_channels_from_env_override(monkeypatch):
    assert channel_watch.channels_from_env(["football"]) == {"football": "coinplayfootballai"}
    monkeypatch.setenv("WATCH_CHANNELS", "football:@mychan,ufc:other")
    assert channel_watch.channels_from_env(["football"]) == {"football": "mychan"}


# ------------------------------------------------------------- цикл watch (канал) --

def test_first_start_does_not_render_history(rendered, monkeypatch):
    _run_channel(monkeypatch, [_page("c", [_pred(1), _pred(2)])])
    assert rendered == []


def test_new_post_after_baseline_rendered_exactly_once(rendered, monkeypatch):
    old = [_pred(1), _pred(2)]
    _run_channel(monkeypatch, [_page("c", old), _page("c", old + [_pred(3)]),
                               _page("c", old + [_pred(3)])])
    assert rendered == ["Home3"]


def test_non_prediction_posts_never_rendered(rendered, monkeypatch):
    _run_channel(monkeypatch, [_page("c", [_pred(1)]),
                               _page("c", [_pred(1), (2, BET_BUILDER), (3, POLL)])])
    assert rendered == []


def test_restart_keeps_baseline(rendered, monkeypatch):
    _run_channel(monkeypatch, [_page("c", [_pred(1)])])
    _run_channel(monkeypatch, [_page("c", [_pred(1), _pred(2)])])  # «перезапуск»
    assert rendered == ["Home2"]


def test_per_cycle_limit_and_publication_order(rendered, monkeypatch):
    monkeypatch.setenv("WATCH_PER_CYCLE", "2")
    new = [_pred(i) for i in (10, 11, 12, 13)]
    _run_channel(monkeypatch, [_page("c", []), _page("c", new)])
    assert rendered == ["Home10", "Home11"]


def test_failures_stop_after_max_attempts(monkeypatch):
    spent = []

    def boom(mt, args):
        spent.append(mt["home"])
        raise RuntimeError("video failed")
    monkeypatch.setattr(generate, "run", boom)
    _run_channel(monkeypatch, [_page("c", [])] + [_page("c", [_pred(9)])] * 5)
    assert spent == ["Home9", "Home9"]  # WATCH_MAX_ATTEMPTS=2 по умолчанию


def test_source_error_does_not_kill_loop(rendered, monkeypatch):
    pages = [_page("c", [_pred(1)]), None, _page("c", [_pred(1), _pred(2)])]
    it = iter(pages)

    def fetch(c):
        p = next(it)
        if p is None:
            raise channel_watch.ChannelError("429")
        return p
    monkeypatch.setattr(channel_watch, "fetch_html", fetch)
    generate.watch(argparse.Namespace(), sleep=lambda s: None, max_cycles=3)
    assert rendered == ["Home2"]


# ----------------------------------------------------------------- источник sets --

def _set(mid, vertical="football", status="ungraded"):
    return {
        "match_id": mid, "vertical": vertical, "status": status,
        "match": {"team_a": f"A{mid}", "team_b": f"B{mid}", "event": "Liga",
                  "start_at": "2999-01-01T18:00:00+00:00"},
        "consensus": {"winner": "team_a", "votes": {"team_a": 1}},
        "predictions": [{"model": "m", "lab": "ChatGPT", "winner": "team_a",
                         "score": {"team_a": 2, "team_b": 1}}],
    }


def _run_sets(monkeypatch, snapshots):
    monkeypatch.setenv("WATCH_SOURCE", "sets")
    monkeypatch.setenv("SETS_API_TOKEN", "t")
    it = iter(snapshots)
    monkeypatch.setattr(coinplay_sets, "fetch_raw", lambda: next(it))
    generate.watch(argparse.Namespace(), sleep=lambda s: None, max_cycles=len(snapshots))


def test_sets_source_baseline_then_new_only(rendered, monkeypatch):
    old = [_set("1"), _set("2")]
    _run_sets(monkeypatch, [{"sets": old}, {"sets": old + [_set("3"), _set("4", "esports"),
                                                            _set("5", status="graded")]}])
    assert rendered == ["A3"]


def test_sets_source_without_token_exits(monkeypatch):
    monkeypatch.setenv("WATCH_SOURCE", "sets")
    with pytest.raises(SystemExit) as e:
        generate.watch(argparse.Namespace(), sleep=lambda s: None, max_cycles=1)
    assert e.value.code == 1


def test_bad_source_exits(monkeypatch):
    monkeypatch.setenv("WATCH_SOURCE", "nope")
    with pytest.raises(SystemExit):
        generate.watch(argparse.Namespace(), sleep=lambda s: None, max_cycles=1)


def test_mark_many_and_markers():
    state.mark_many({"a", "b"})
    assert state.already_posted({"a", "b", "c"}) == {"a", "b"}
    assert not state.has_marker("x")
    state.set_marker("x")
    assert state.has_marker("x")


# ----------------------------------------------------------- чат по вертикали --

def _sent_chat(monkeypatch, tmp_path, vertical):
    posted = []

    class R:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(generate.requests, "post",
                        lambda url, data=None, **kw: posted.append(data["chat_id"]) or R())
    z = tmp_path / "k.zip"
    z.write_bytes(b"x")
    generate.send_telegram(str(z), {"home": "A", "away": "B", "vertical": vertical})
    return posted


def test_kit_goes_to_vertical_chat_else_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100base")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_UFC", "-100ufc")
    assert _sent_chat(monkeypatch, tmp_path, "ufc") == ["-100ufc"]
    assert _sent_chat(monkeypatch, tmp_path, "esports") == ["-100base"]
    assert _sent_chat(monkeypatch, tmp_path, "") == ["-100base"]


def test_default_watch_verticals_are_all_three(rendered, monkeypatch):
    monkeypatch.delenv("WATCH_VERTICALS")
    pages = {"coinplayfootballai": _page("coinplayfootballai", [_pred(1)]),
             "coinplayesportai": _page("coinplayesportai", [_pred(2)]),
             "coinplayufcai": _page("coinplayufcai", [_pred(3)])}
    seen = []
    monkeypatch.setattr(channel_watch, "fetch_html", lambda c: seen.append(c) or pages[c])
    generate.watch(argparse.Namespace(), sleep=lambda s: None, max_cycles=1)
    assert sorted(seen) == sorted(pages)


# ------------------------------------------- видео-кит: площадки и превью --

def _video_kit(tmp_path):
    cover = tmp_path / "c.jpg"
    from PIL import Image
    Image.new("RGB", (60, 100), "black").save(cover)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"vid")
    rows = [{"label": "ChatGPT", "home": 2, "away": 1}]
    z = generate.build_kit(str(tmp_path), "a-vs-b",
                           {"home": "A", "away": "B", "competition": "PL", "date": "2026-10-01"},
                           rows, str(vid), str(cover))
    import json as _j, zipfile as _z
    with _z.ZipFile(z) as zf:
        return _j.loads(zf.read("kit.json")), zf.namelist()


def test_video_kit_is_instagram_only_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_PLATFORMS", raising=False)
    kit, names = _video_kit(tmp_path)
    assert list(kit["platforms"]) == ["instagram"]
    assert "instagram/video.mp4" in names
    assert not any(n.startswith(("threads/", "x/")) for n in names)


def test_video_platforms_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PLATFORMS", "instagram,threads,bogus")
    kit, _ = _video_kit(tmp_path)
    assert list(kit["platforms"]) == ["instagram", "threads"]


def test_preview_not_sent_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100base")
    monkeypatch.delenv("TELEGRAM_SEND_PREVIEW", raising=False)
    urls = []

    class R:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(generate.requests, "post",
                        lambda url, **kw: urls.append(url.rsplit("/", 1)[1]) or R())
    z = tmp_path / "k.zip"
    z.write_bytes(b"x")
    v = tmp_path / "p.mp4"
    v.write_bytes(b"v")
    generate.send_telegram(str(z), {"home": "A", "away": "B"}, str(v))
    assert urls == ["sendDocument"]
    monkeypatch.setenv("TELEGRAM_SEND_PREVIEW", "true")
    urls.clear()
    generate.send_telegram(str(z), {"home": "A", "away": "B"}, str(v))
    assert urls == ["sendVideo", "sendDocument"]


# ------------------------------------------ Telegram: токен не в ошибках --

def test_telegram_error_hides_token_and_shows_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100x")

    class Resp:
        status_code = 400
        def json(self):
            return {"description": "Bad Request: chat not found"}

    def boom(url, **kw):
        e = generate.requests.HTTPError(f"400 for url: {url}")
        e.response = Resp()
        raise e
    monkeypatch.setattr(generate.requests, "post", boom)
    z = tmp_path / "k.zip"
    z.write_bytes(b"x")
    import pytest
    with pytest.raises(RuntimeError) as ei:
        generate.send_telegram(str(z), {"home": "A", "away": "B"})
    msg = str(ei.value)
    assert "SECRET" not in msg and "chat not found" in msg and "sendDocument" in msg


def test_retry_does_not_pay_for_video_twice(monkeypatch, tmp_path):
    paid = []

    def fake_segment(blank, filled, prompt, path):
        paid.append(path)
        open(path, "wb").write(b"seg")

    def fake_assemble(parts, out, **kw):
        open(out, "wb").write(b"vid")
        return out
    monkeypatch.setattr(generate.video, "ensure_tools", lambda: None)
    monkeypatch.setattr(generate.video, "generate_segment", fake_segment)
    monkeypatch.setattr(generate.video, "assemble", fake_assemble)
    monkeypatch.setattr(generate.video, "pick_music", lambda seed="": None)
    monkeypatch.setenv("ALLOW_PAST_DATES", "true")
    monkeypatch.setenv("AI_BACKGROUND", "0")
    base = {"id": "k1", "home": "A", "away": "B", "date": "2026-10-01", "vertical": "",
            "scores": "2-1,1-0,1-0,2-1,2-0"}
    args = argparse.Namespace(out=str(tmp_path), no_video=False, no_send=True,
                              keep_temp=False, no_ai_background=True)
    generate.run(dict(base), args)
    generate.run(dict(base), args)          # повтор: видео уже есть
    assert len(paid) == 1
    generate.run({**base, "scores": "0-1,0-1,0-1,0-1,0-1"}, args)  # другие счета
    assert len(paid) == 2


def test_youtube_caption_and_kit(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PLATFORMS", "instagram,youtube")
    kit, names = _video_kit(tmp_path)
    assert list(kit["platforms"]) == ["instagram", "youtube"]
    import zipfile as _z
    z = tmp_path / "a-vs-b.zip"
    with _z.ZipFile(z) as zf:
        text = zf.read("youtube/post.txt").decode()
    first, _, rest = text.partition("\n")
    assert first.startswith("🤖") and "A vs B" in first and len(first) <= 100
    assert "Link in bio" not in text and text.rstrip().endswith("#Shorts #AI #predictions")


def _audio_peaks(path, t0, t1):
    import subprocess, struct
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t0), "-t", str(t1 - t0), "-i", path,
                          "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return max((abs(v) for v in vals), default=0)


def test_hold_clip_has_no_stinger_by_default(tmp_path, monkeypatch):
    import subprocess
    from video import assemble
    seg = tmp_path / "seg.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=270x480:d=1",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(seg)], check=True)
    monkeypatch.delenv("HOLD_STINGER", raising=False)
    out = assemble([str(seg)], str(tmp_path / "off.mp4"), hold_sec=1.0)
    assert _audio_peaks(out, 1.0, 2.0) < 50          # тишина в стоп-кадре
    monkeypatch.setenv("HOLD_STINGER", "true")
    out2 = assemble([str(seg)], str(tmp_path / "on.mp4"), hold_sec=1.0)
    assert _audio_peaks(out2, 1.0, 2.0) > 1000       # «дзынь» возвращается по флагу

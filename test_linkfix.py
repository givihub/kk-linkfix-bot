"""Тесты логики преобразования ссылок: python3 test_linkfix.py."""
import linkfix
from linkfix import convert


def test_instagram_reel():
    r = convert("https://www.instagram.com/reel/DaNx4QjtvH9/")
    assert r.original == "https://www.instagram.com/reel/DaNx4QjtvH9/"
    assert r.embed == "https://kkinstagram.com/reel/DaNx4QjtvH9/"
    assert r.platform == "instagram" and r.label == "Instagram"


def test_instagram_no_www():
    r = convert("https://instagram.com/p/ABC123/")
    assert r.embed == "https://kkinstagram.com/p/ABC123/"
    assert r.original == "https://www.instagram.com/p/ABC123/"


def test_instagram_profile_ignored():
    assert convert("https://www.instagram.com/someuser/") is None


def test_kkinstagram_back_to_www():
    r = convert("https://kkinstagram.com/reel/DaNx4QjtvH9/")
    assert r.original == "https://www.instagram.com/reel/DaNx4QjtvH9/"


def test_tiktok_short_t():
    r = convert("https://www.tiktok.com/t/ZP8tDesMB/")
    assert r.original == "https://www.tiktok.com/t/ZP8tDesMB/"
    assert r.embed == "https://kktiktok.com/t/ZP8tDesMB/"
    assert r.platform == "tiktok" and r.label == "TikTok"


def test_tiktok_vm():
    r = convert("https://vm.tiktok.com/ZP8tDesMB/")
    assert r.original == "https://www.tiktok.com/t/ZP8tDesMB/"
    assert r.embed == "https://kktiktok.com/t/ZP8tDesMB/"


def test_tiktok_full_video():
    r = convert("https://www.tiktok.com/@user123/video/7300000000000000000")
    assert r.embed == "https://kktiktok.com/@user123/video/7300000000000000000"


def test_tiktok_root_ignored():
    assert convert("https://www.tiktok.com/") is None


def test_x_status():
    r = convert("https://x.com/elonmusk/status/1234567890123456789")
    assert r.original == "https://x.com/elonmusk/status/1234567890123456789"
    assert r.embed == "https://fixupx.com/elonmusk/status/1234567890123456789"
    assert r.platform == "x" and r.label == "𝕏"


def test_twitter_legacy_domain():
    r = convert("https://twitter.com/user/status/111?s=20")
    assert r.embed == "https://fixupx.com/user/status/111"
    assert r.original == "https://x.com/user/status/111"


def test_x_profile_ignored():
    assert convert("https://x.com/elonmusk") is None


def test_reddit_not_supported():
    assert convert("https://www.reddit.com/r/videos/comments/abc123/some_title/") is None
    assert convert("https://redd.it/abc123") is None


def test_bare_url_without_scheme():
    r = convert("instagram.com/reel/XYZ/")
    assert r is not None and r.embed == "https://kkinstagram.com/reel/XYZ/"


def test_unrelated():
    assert convert("https://example.com/reel/nope/") is None
    assert convert("https://youtube.com/watch?v=abc") is None


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except AssertionError:
                failed += 1
                print(f"FAIL {name}")
    sys.exit(1 if failed else 0)

"""حارسُ تدوير اعتماد عامل tradingActivity الأمامي.

العاملُ طويل العمر؛ Privy يدوّر access_token على القرص، فلا يجوز أن يحتفظ العميل
المبني عند الإقلاع بالتوكن القديم إلى الأبد. العطب الحي (2026-08-29): العملية
Running منذ 10:48، لكن آخر نجاح 2026-08-27 وآخر خطأ كل خمس دقائق UnauthorizedError.
"""
import run_activity_head as head


class _Client:
    def __init__(self, token: str, *, close_error: bool = False) -> None:
        self.token = token
        self.closed = False
        self.close_error = close_error

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("close failed")


async def test_unchanged_token_keeps_the_current_client(monkeypatch):
    old = _Client("same")
    monkeypatch.setattr(head, "_load_access_token", lambda: "same")
    monkeypatch.setattr(head, "_build_client", lambda token: (_ for _ in ()).throw(
        AssertionError("لا يُبنى عميل جديد إذا لم يتغير التوكن")
    ))

    client, token = await head._maybe_rotate_client(old, "same")

    assert client is old
    assert token == "same"
    assert old.closed is False


async def test_changed_token_rebuilds_and_closes_the_old_client(monkeypatch):
    old = _Client("old")
    fresh = _Client("new")
    monkeypatch.setattr(head, "_load_access_token", lambda: "new")
    monkeypatch.setattr(head, "_build_client", lambda token: fresh)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is fresh
    assert token == "new"
    assert old.closed is True


async def test_changed_token_rebuilds_even_if_old_client_close_fails(monkeypatch):
    """إغلاق النقل القديم تنظيفٌ فقط؛ لا يجوز أن يهدر عميلًا طازجًا بُني بنجاح."""
    old = _Client("old", close_error=True)
    fresh = _Client("new")
    monkeypatch.setattr(head, "_load_access_token", lambda: "new")
    monkeypatch.setattr(head, "_build_client", lambda token: fresh)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is fresh
    assert token == "new"
    assert old.closed is True


async def test_token_read_error_does_not_destroy_a_working_client(monkeypatch):
    """عطب قراءة عابر لا يمنع الدورة ما دام العميل الحالي ما يزال صالحًا."""
    old = _Client("old")

    def fail_load():
        raise OSError("sharing violation")

    monkeypatch.setattr(head, "_load_access_token", fail_load)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is old
    assert token == "old"
    assert old.closed is False


async def test_missing_token_does_not_destroy_a_working_client(monkeypatch):
    """كتابة الملف الذرّية قد تُرى بين rename؛ غياب عابر لا يرمي العميل الحالي."""
    old = _Client("old")
    monkeypatch.setattr(head, "_load_access_token", lambda: None)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is old
    assert token == "old"
    assert old.closed is False

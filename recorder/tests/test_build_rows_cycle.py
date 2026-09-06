"""Tests of the scheduled training-rows build cycle (no network, no real database).

What is tested is `run_cycle`, not `build`: the batching and cap logic is what
the launcher added, and it is what could drift silently. `build` itself is
covered in test_features / test_flow_and_coverage.
"""
import config
import pytest
import run_build_rows


class _FakeBuild:
    """A `build` stand-in returning programmed counts and recording the limit
    of each call."""

    def __init__(self, per_call):
        self.per_call = list(per_call)
        self.takes = []

    def __call__(self, _db, rebuild, limit, dry, candidates_only):
        assert rebuild is False, "the launcher must never pass rebuild"
        assert dry is False
        assert candidates_only is False
        self.takes.append(limit)
        built = self.per_call.pop(0) if self.per_call else 0
        return {"built": built, "skipped_no_event": 0, "rows": [{}] * built}


@pytest.fixture()
def patched(monkeypatch):
    def _apply(per_call, batch=500, cap=4000):
        fake = _FakeBuild(per_call)
        monkeypatch.setattr("build_training_rows.build", fake)
        monkeypatch.setattr(config, "BUILD_ROWS_BATCH", batch)
        monkeypatch.setattr(config, "BUILD_ROWS_MAX_PER_CYCLE", cap)
        return fake

    return _apply


def test_stops_when_pending_exhausted(patched):
    """A short batch means the pending is exhausted — we stop without an extra
    call."""
    fake = patched([500, 120], batch=500, cap=4000)
    stats = run_build_rows.run_cycle(None)
    assert stats == {"built": 620, "skipped_no_event": 0, "batches": 2}
    assert fake.takes == [500, 500]


def test_respects_per_cycle_cap(patched):
    """The cap chews a large backlog across cycles instead of locking the
    database in one go."""
    fake = patched([500] * 10, batch=500, cap=1500)
    stats = run_build_rows.run_cycle(None)
    assert stats["built"] == 1500
    assert stats["batches"] == 3
    assert fake.takes == [500, 500, 500]


def test_last_batch_is_clipped_to_cap(patched):
    """The last batch does not exceed what remains of the cap."""
    fake = patched([500, 500], batch=500, cap=700)
    run_build_rows.run_cycle(None)
    assert fake.takes == [500, 200]


def test_no_pending_is_a_single_cheap_call(patched):
    """Nothing pending ⇒ one call then exit (the normal case every hour)."""
    fake = patched([0], batch=500, cap=4000)
    stats = run_build_rows.run_cycle(None)
    assert stats == {"built": 0, "skipped_no_event": 0, "batches": 1}
    assert fake.takes == [500]


def test_rows_are_not_accumulated(patched):
    """Rows are discarded after each batch — the process lives for days, and
    stacking them is a leak."""
    patched([500, 10], batch=500, cap=4000)
    stats = run_build_rows.run_cycle(None)
    assert "rows" not in stats

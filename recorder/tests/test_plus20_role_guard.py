"""Role guard test: time_to_plus20_min can never be an entry rule.

The verdict is shared (the owner's measurement of 2026-08-28 over the full
history): entering after +20% is a net loser in every window (-1% to -11%)
versus +0.74% at the signal. This test pins the decision in the code itself:
any future use of the column as a training target with late entry or as a
buy threshold breaks this test on purpose — forcing its author to read the
measurement first.
"""
import labeler


def test_plus20_is_veto_filter_never_entry():
    """The contract declared in the labeler's docstring: a drop filter, not an entry.

    We verify that the living documentation (the compute_labels docstring)
    states the verdict explicitly — it is the first thing anyone editing
    that file reads.
    """
    import inspect

    src = inspect.getsource(labeler.compute_labels)
    assert "not an entry rule" in src or "never an entry rule" in src, (
        "The role warning was removed from compute_labels — entering after "
        "+20% is a net loser (-1% to -11%, measurement of 2026-08-28). "
        "Restore the documentation or change the decision with a new "
        "counter-measurement."
    )


def test_config_plus20_comment_declares_veto_role():
    """The threshold constant is documented with its limits: a filter, not an entry."""
    import inspect

    import config

    src = inspect.getsource(config)
    assert "not an entry rule" in src or "never an entry rule" in src

"""The key-file format: read by three processes, written by the dashboard.

The tests here never touch a real key or the real file — `tmp_path` always.
"""
import json

import key_file


def test_tail_shows_four_characters_and_nothing_more():
    """The FR-013 allowed reveal is capped at four characters — no fifth, no
    length."""
    assert key_file.tail("abcdefghijkl-WXYZ") == "WXYZ"
    assert len(key_file.tail("x" * 64)) == 4


def test_tail_refuses_short_keys_because_four_of_twelve_is_a_third_of_the_secret():
    """A short key: the reveal is dropped and the secret stays — no half
    solution in between."""
    assert key_file.tail("short") == ""
    assert key_file.tail("x" * 11) == ""
    assert key_file.tail("x" * 12) == "xxxx"


def test_load_treats_a_missing_or_broken_file_as_empty():
    """The reader does not stumble: a machine without keys is an ordinary
    state, and corruption is the writer's responsibility."""
    assert key_file.load("no-such-file.json") == {}


def test_load_treats_a_corrupt_file_as_empty(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert key_file.load(str(path)) == {}

    path.write_text('["list at top level"]', encoding="utf-8")
    assert key_file.load(str(path)) == {}


def test_entries_reads_all_three_shapes_and_defaults_enabled_to_true():
    """The existing file was written by hand before multiplicity existed; a
    hand-written line without `enabled` counts as enabled."""
    plain = key_file.entries({"helius_api_key": " one "}, "helius_api_keys", "helius_api_key")
    assert plain == [{"key": "one", "label": "", "enabled": True}]

    listed = key_file.entries(
        {"helius_api_keys": ["a", {"key": "b", "label": "second account"}]},
        "helius_api_keys", "helius_api_key",
    )
    assert [row["key"] for row in listed] == ["a", "b"]
    assert [row["enabled"] for row in listed] == [True, True]
    assert listed[1]["label"] == "second account"


def test_entries_keeps_disabled_rows_for_the_dashboard_to_re_enable():
    """A disabled row stays visible, otherwise temporary disabling has no way
    back."""
    rows = key_file.entries(
        {"k": [{"key": "a", "enabled": False}, "b"]}, "k", "k1",
    )
    assert [(row["key"], row["enabled"]) for row in rows] == [("a", False), ("b", True)]


def test_entries_drops_blanks_and_duplicates():
    rows = key_file.entries(
        {"k": ["a", " a ", "", {"key": ""}, None, 7, {"key": "b"}]}, "k", "k1",
    )
    assert [row["key"] for row in rows] == ["a", "b"]


def test_an_empty_plural_list_hides_the_legacy_singular():
    """Deleting the last key must stay deleted — the singular does not come
    back from its grave."""
    assert key_file.entries(
        {"k": [], "k1": "old-value"}, "k", "k1",
    ) == []


def test_save_entries_round_trips_and_drops_the_legacy_singular(tmp_path):
    """The singular is deleted on the first write: one source of truth, not
    two that drift apart."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"helius_api_key": "old", "other_setting": 7}), encoding="utf-8")

    key_file.save_entries(str(path), "helius_api_keys", "helius_api_key", [
        {"key": "new-one-123456", "label": "main"},
        {"key": "new-two-123456", "label": "", "enabled": False},
    ])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "helius_api_key" not in data
    # What this provider does not own is not touched: the file is shared by
    # three providers and other settings.
    assert data["other_setting"] == 7
    rows = key_file.load_entries(str(path), "helius_api_keys", "helius_api_key")
    assert [(row["key"], row["label"], row["enabled"]) for row in rows] == [
        ("new-one-123456", "main", True),
        ("new-two-123456", "", False),
    ]


def test_save_entries_leaves_other_providers_untouched(tmp_path):
    path = tmp_path / "keys.json"
    key_file.save_entries(str(path), "helius_api_keys", "helius_api_key",
                          [{"key": "helius-key-1234"}])
    key_file.save_entries(str(path), "nodereal_api_keys", "nodereal_api_key",
                          [{"key": "nodereal-key-12"}])

    assert [r["key"] for r in key_file.load_entries(
        str(path), "helius_api_keys", "helius_api_key")] == ["helius-key-1234"]
    assert [r["key"] for r in key_file.load_entries(
        str(path), "nodereal_api_keys", "nodereal_api_key")] == ["nodereal-key-12"]


def test_save_entries_replaces_atomically_and_leaves_no_temp_file(tmp_path):
    """The recorder reads this file on **every call**; a half-written file =
    a full outage."""
    path = tmp_path / "keys.json"
    key_file.save_entries(str(path), "k", "k1", [{"key": "value-123456"}])
    key_file.save_entries(str(path), "k", "k1", [{"key": "value-123456"},
                                                 {"key": "second-123456"}])

    assert [p.name for p in tmp_path.iterdir()] == ["keys.json"]
    assert path.read_text(encoding="utf-8").endswith("\n")


def _source(name: str) -> str:
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, name), encoding="utf-8") as fh:
        return fh.read()


def test_probe_endpoints_match_the_clients_that_use_the_keys():
    """Probing a different endpoint would say "healthy" about a key that does
    not work where it is actually used.

    We match the host, not the full URL: the client builds its own path (key
    in the query string, or the path, or a header) — the host is what must
    not drift. And reading the source as text is deliberate: the URLs in the
    clients are strings inside functions, not module constants, and a textual
    check catches drift without restructuring a client that works today.
    """
    import config

    assert "helius-rpc.com" in key_file.PROVIDERS["helius"]["probe"]["url"]
    assert "helius-rpc.com" in config.SOLANA_RPC_URL
    assert "nodereal.io" in key_file.PROVIDERS["nodereal"]["probe"]["url"]
    assert "nodereal.io" in _source("nodereal_rpc.py")
    audit = _source("audit_evm_ledger.py")
    assert "g.alchemy.com" in key_file.PROVIDERS["alchemy"]["probe"]["url"]
    assert "g.alchemy.com" in audit
    assert "lb.drpc.org" in key_file.PROVIDERS["drpc"]["probe"]["url"]
    assert "lb.drpc.org" in audit


def test_provider_field_names_match_what_read_keys_is_actually_called_with():
    """A field name differing by one letter = a dashboard writing to a place
    nobody reads.

    We check the `read_keys` calls in everything that reads keys: every
    (plural, singular) pair actually called must be known to `PROVIDERS`,
    otherwise a provider is read by the recorder but unseen by the dashboard —
    or worse: the dashboard writes a field nobody reads and the key looks
    added.
    """
    import re

    known = {(meta["plural"], meta["singular"]) for meta in key_file.PROVIDERS.values()}
    pattern = re.compile(r"""read_keys\(\s*["'](\w+)["'],\s*["'](\w+)["']""")
    found = set()
    for name in ("solana_rpc.py", "nodereal_rpc.py", "run_chain.py",
                 "run_evm_replay.py", "audit_evm_ledger.py"):
        found |= {tuple(match) for match in pattern.findall(_source(name))}

    assert found, "no read_keys calls found — the call shape changed and this check is blind"
    assert found <= known, f"fields the dashboard does not know: {found - known}"


def test_the_audit_tools_field_triplets_are_the_same_ones_the_dashboard_writes():
    """The audit tool calls `read_keys` with variables from its own table, so
    the textual check above is blind to it.

    The call check above reads the letters between parentheses, while
    `audit_evm_ledger` passes field names from `PROVIDER_FIELDS` — that
    pattern never sees them. So the two tables are matched directly: one
    letter off here means a dashboard writing a key into a field the auditor
    never reads, and the key looks added when it is not there.
    """
    import audit_evm_ledger

    for name, (plural, singular, env) in audit_evm_ledger.PROVIDER_FIELDS.items():
        assert name in key_file.PROVIDERS, name
        meta = key_file.PROVIDERS[name]
        assert (meta["plural"], meta["singular"], meta["env"]) == (
            plural, singular, env,
        ), name
    # And every network's route in the auditor is a registered provider — no
    # name invented at runtime.
    for network, routes in audit_evm_ledger.ARCHIVE_ROUTES.items():
        for provider, _ in routes:
            assert provider in key_file.PROVIDERS, (network, provider)


def test_every_provider_has_the_five_fields_the_dashboard_reads():
    """The dashboard reads all five without protection, so a provider missing
    a field takes it down with a KeyError."""
    for name, meta in key_file.PROVIDERS.items():
        assert set(meta) == {"plural", "singular", "env", "title", "probe"}, name
        assert meta["probe"]["method"] == "POST", name
        assert "{key}" in meta["probe"]["url"], name
        assert meta["probe"]["json"]["method"], name

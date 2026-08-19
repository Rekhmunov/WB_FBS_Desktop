"""WB FBS hidden tabs: finished / cancelled / archive (disabled for all roles)."""

from review_processor.wb_fbs import (
    HIDDEN_TABS,
    OWNER_ONLY_TABS,
    TAB_ARCHIVE,
    TAB_CANCELLED,
    TAB_FINISHED,
    TAB_NEW,
    is_hidden_tab,
    is_owner_only_tab,
)


def test_hidden_tabs_set():
    expected = frozenset({TAB_FINISHED, TAB_CANCELLED, TAB_ARCHIVE})
    assert HIDDEN_TABS == expected
    assert OWNER_ONLY_TABS == expected


def test_is_hidden_tab():
    assert is_hidden_tab("finished")
    assert is_hidden_tab("CANCELLED")
    assert is_hidden_tab(" archive ")
    assert not is_hidden_tab(TAB_NEW)
    assert not is_hidden_tab("assembly")
    assert not is_hidden_tab("")
    assert not is_hidden_tab(None)


def test_is_owner_only_tab_alias():
    assert is_owner_only_tab("finished")
    assert is_owner_only_tab("cancelled")
    assert not is_owner_only_tab(TAB_NEW)

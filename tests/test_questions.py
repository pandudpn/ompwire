"""The option marker the model is asked to end its turn with."""

from __future__ import annotations

from ompwire.questions import MAX_OPTIONS, extract, hide_partial


class TestExtract:
    def test_marker_becomes_options_and_leaves_the_text(self):
        text, options = extract("""Which database?

<<options: postgres | mysql | sqlite>>""")
        assert text == "Which database?"
        assert options == ["postgres", "mysql", "sqlite"]

    def test_newlines_separate_options_too(self):
        _text, options = extract("""pick
<<options:
redis
valkey
>>""")
        assert options == ["redis", "valkey"]

    def test_marker_is_case_insensitive_and_tolerates_spacing(self):
        _text, options = extract("<< OPTION :  a  |  b >>")
        assert options == ["a", "b"]

    def test_duplicates_collapse_in_order(self):
        _text, options = extract("<<options: a | b | a>>")
        assert options == ["a", "b"]

    def test_at_most_six_options(self):
        body = " | ".join(f"opt{index}" for index in range(12))
        _text, options = extract(f"<<options: {body}>>")
        assert len(options) == MAX_OPTIONS

    def test_no_marker_changes_nothing(self):
        text, options = extract("just prose, no marker")
        assert text == "just prose, no marker"
        assert options == []

    def test_an_empty_marker_is_not_an_option(self):
        text, options = extract("""question
<<options:  >>""")
        assert options == []
        assert text == "question"

    def test_text_around_the_marker_is_kept_and_stripped(self):
        text, options = extract("before <<options: yes | no>> after")
        assert text == "before  after"
        assert options == ["yes", "no"]


class TestHidePartial:
    def test_a_half_written_marker_is_hidden_while_streaming(self):
        assert hide_partial("""Which one?
<<options: a | b""", final=False) == "Which one?"

    def test_a_complete_marker_is_replaced_by_nothing_while_streaming(self):
        assert hide_partial("""Choose
<<options: a | b>>""", final=False) == "Choose"

    def test_final_text_drops_the_marker(self):
        assert hide_partial("""Choose
<<options: a | b>>""", final=True) == "Choose"

    def test_plain_text_passes_through(self):
        assert hide_partial("no marker here", final=False) == "no marker here"

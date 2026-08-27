"""The dialog a backend's mid-run question is asked in.

These build the real dialog rather than a stand-in, because what is being
checked is what a screen reader would read out: one radio button per answer,
a checked list where several answers are allowed, and a text box that appears
when "Other" is chosen. They skip themselves where no screen is available, so
a headless machine does not fail on a window it could never have opened.
"""

from __future__ import annotations

import pytest

from agent_backends import Question, QuestionOption

wx = pytest.importorskip("wx")


@pytest.fixture(scope="module")
def wx_app():
    try:
        application = wx.App(False)
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no display for wxPython: {exc}")
    yield application


@pytest.fixture
def frame(wx_app):
    window = wx.Frame(None)
    try:
        yield window
    finally:
        window.Destroy()


QUESTIONS = (
    Question(
        question="Tabs or spaces?",
        header="Indent",
        options=(
            QuestionOption("Tabs", "Tab characters - one per level."),
            QuestionOption("Spaces", "Four spaces per level."),
        ),
    ),
    Question(
        question="Which extras?",
        header="Extras",
        multi_select=True,
        options=(
            QuestionOption("Tests", "Add tests."),
            QuestionOption("Docs", "Add docs."),
        ),
    ),
)


def _dialog(frame, questions=QUESTIONS):
    import blindpilot_app

    return blindpilot_app.QuestionDialog(frame, "claude", questions)


def test_one_answer_gets_radio_buttons_and_several_gets_a_checked_list(frame):
    dlg = _dialog(frame)
    try:
        single, several = dlg._pickers
        assert isinstance(single, wx.RadioBox)
        assert isinstance(several, wx.CheckListBox)
        # Each answer reads as one line: the label, then what choosing it means.
        assert single.GetString(0) == "Tabs: Tab characters - one per level."
        # Every backend leaves the "Other" answer to the client, so the dialog
        # is where it is added - once, at the end of each list.
        assert single.GetString(single.GetCount() - 1) == dlg.OTHER
        assert several.GetString(several.GetCount() - 1) == dlg.OTHER
    finally:
        dlg.Destroy()


def test_answers_come_back_as_the_backends_own_labels(frame):
    dlg = _dialog(frame)
    try:
        single, several = dlg._pickers
        single.SetSelection(1)
        several.Check(0, True)
        several.Check(1, True)
        # Not the line the dialog drew: the label the backend asked with.
        assert dlg.answers() == [["Spaces"], ["Tests", "Docs"]]
    finally:
        dlg.Destroy()


def test_choosing_other_opens_a_box_to_type_in(frame):
    dlg = _dialog(frame)
    try:
        single, _several = dlg._pickers
        assert not dlg._texts[0].IsShown()

        single.SetSelection(single.GetCount() - 1)
        dlg._on_choice(wx.CommandEvent())

        assert dlg._texts[0].IsShown()
        dlg._texts[0].SetValue("  two spaces, always  ")
        assert dlg.answers()[0] == ["two spaces, always"]
    finally:
        dlg.Destroy()


def test_a_typed_answer_can_be_added_to_the_ones_on_the_list(frame):
    dlg = _dialog(frame)
    try:
        _single, several = dlg._pickers
        several.Check(0, True)
        several.Check(several.GetCount() - 1, True)
        dlg._on_choice(wx.CommandEvent())
        dlg._texts[1].SetValue("Benchmarks")

        assert dlg.answers()[1] == ["Tests", "Benchmarks"]
    finally:
        dlg.Destroy()


def test_a_question_that_takes_no_typed_answer_does_not_offer_one(frame):
    fixed = (
        Question(
            question="Which one?",
            options=(QuestionOption("A"), QuestionOption("B")),
            allow_custom=False,
        ),
    )
    dlg = _dialog(frame, fixed)
    try:
        (picker,) = dlg._pickers
        assert [picker.GetString(item) for item in range(picker.GetCount())] == ["A", "B"]
        assert dlg._wants_custom(0) is False
    finally:
        dlg.Destroy()

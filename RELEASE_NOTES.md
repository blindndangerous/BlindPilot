# BlindPilot 0.6.1

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## A conversation survives the questions it asked

Answering a question on the opencode backend could leave the whole conversation
permanently broken. The provider refused the stored question step when it rebuilt the
conversation for the next request — "Invalid assistant message: content or tool_calls
must be set" — and because that rebuild happens on every step, every following message
failed with the same error no matter what was typed. Three "continue" attempts became
three identical failures.

BlindPilot now recognises that refusal when a question was answered during the turn,
removes the broken step and everything after it, and sends the message again on the
repaired conversation. The transcript keeps its row saying what was asked and what was
answered, so nothing the person said is lost, and the turn carries on instead of
ending. Only one repair is attempted per turn: if the provider refuses the conversation
again, that second refusal is reported rather than looping. The same refusal with no
question in the turn — or where the question was dismissed rather than answered — is
still reported as the plain failure it is.

# BlindPilot 0.6.0

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## Answering the questions a backend stops to ask

Every backend BlindPilot drives can pause a turn to put a multiple-choice question to the
person driving it. Until now none of those questions ever reached anybody: they were
declined, ignored, or never offered in the first place. They are now asked properly.

A question opens a dialog. Where one answer is wanted there is one radio button per
answer, each read as its label and then what choosing it means. Where several are allowed
there is a checked list. The last choice in every list is "Other: type your own answer",
and picking it opens a box to type in. Closing the dialog tells the backend the question
went unanswered, so a turn is never left waiting on an answer that is not coming, and
stopping a run closes an open question with it. The transcript keeps a row saying what was
asked and what was said.

Each backend is handled in its own terms. Claude Code's AskUserQuestion arrives on the
permission channel of the stream BlindPilot is already reading, and headless Claude Code is
now told that this app can show a prompt — without that the tool is not offered at all,
which is why Claude could never ask before. Codex's `request_user_input` is switched on for
the app server BlindPilot starts and answered by question id. opencode's `question.asked`
event is replied to instead of rejected. FreeBuff, which has no API of any kind, has its
own question box read off its terminal and driven with the keys it understands.

## FreeBuff reaches its composer again

FreeBuff no longer labels the model on its start screen "RECOMMENDED", and BlindPilot
waited for that word before answering the chooser. On current FreeBuff every message
therefore sat behind a start screen nobody could see, and the turn never began. The start
screen is now recognised however it is worded.

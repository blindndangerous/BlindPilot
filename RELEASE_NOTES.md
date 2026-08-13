# BlindPilot 0.3.11

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

## Arrow keys stay in the responses

- Pressing Down on the newest response used to drop focus into the prompt. Nothing on
  Windows behaves that way: a list keeps focus at its end, and a screen reader user
  expects to hear the last row again rather than find themselves typing. Down on the
  last row now stays on the last row, in the list and in the read-only edit field
  alike, and Up on the first row stays on the first row.
- Tab is the way from the responses to the prompt, and Up from the prompt's first line
  is still the way back. The prompt's hint says so.

## Reading is no longer interrupted by the answer still arriving

- The responses list is rebuilt whenever new output arrives, and rebuilding a Windows
  list clears its selection — so an answer that was still streaming kept throwing you
  out of the row you were reading. The row you are on is now kept across every refresh.
- A long, chatty job could post thousands of GUI events faster than the list and the
  screen reader could consume them, and arrow keys went unanswered while it did.
  Backend output is now applied in small batches that yield to keyboard and
  accessibility events between them, and the list is redrawn once per batch rather than
  once per line of output.

## FreeBuff stays on DeepSeek V4 Pro

- FreeBuff renamed the model to "DeepSeek V4 Pro 08/13" in its program but left its
  documentation calling it "DeepSeek V4 Pro", and BlindPilot read the two as different
  models. Pro therefore vanished from the model list altogether, which disabled both of
  the safeguards that kept it selected — so BlindPilot fell back to reading FreeBuff's
  own setting, which FreeBuff rewrites to Flash after every turn. Whole conversations
  ran on Flash without saying so.
- A release date on a model name is now understood as the same model, so Pro is found,
  listed first, and chosen. This also settles what the tools were running on: FreeBuff
  picks its helper agents to match the model it is on, so a session on Flash announced
  a reviewer named for Flash.
- Replacing a FreeBuff terminal mid-message now re-applies the chosen model first. The
  terminal being replaced is a FreeBuff, and one rewrites that setting as it exits.

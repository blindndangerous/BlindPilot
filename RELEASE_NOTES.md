# BlindPilot 0.5.0

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## Sessions are real tabs

The window's session book is a native tab control now, which means a screen reader reads
it as one: "tab 2 of 4", with the name of the conversation in that tab. **Ctrl+Tab** moves
to the next session and **Ctrl+Shift+Tab** to the previous one, from anywhere in the
window — including from inside the prompt box, where you spend most of your time. The
Mac-standard **Ctrl+Shift+]** and **Ctrl+Shift+[** still work, and **Ctrl+1** to
**Ctrl+9** still jump straight to a tab.

The session dropdown above the tabs stays exactly where it was. It is the fastest way to
reach tab 9 of 12 without stepping through the eight in between.

## A tab is named after the conversation in it

Sending the first message names the conversation — that is the title Recent Conversations
lists it under — and from that moment the tab carries the same name. Reopening a past
conversation names its tab too. A tab whose conversation has not started yet, or that has
just been cleared with **Ctrl+Shift+N**, falls back to showing its folder.

Two tabs open on the same project are no longer two tabs with the same name.

## Every backend starts fully automatic

Bypass-permissions is where a new tab starts, for every backend that has permission modes,
and it is where **Ctrl+Shift+M** returns to. A run that stops mid-task to ask for
permission is a run waiting on someone who has no way to know it is waiting.

If you have used BlindPilot before, your saved mode is moved onto full auto once, on the
first launch after upgrading. A mode you choose in the picker afterwards is yours, and is
left exactly where you put it.

## Fixes

- **Background tabs no longer speak over the tab you are reading.** The check for "is this
  the tab in front" tested for a control the window did not actually use, so it never
  matched and every running session narrated at once.
- **Arrowing along the tab strip keeps focus on the strip.** Changing page moved focus into
  the prompt, so the second arrow press never reached the tabs.
- **Opening a session no longer points the session dropdown at a row it does not have yet.**
- **The permission picker is greyed out from what the backend actually supports**, rather
  than from FreeBuff's name, and explains itself in that backend's terms.

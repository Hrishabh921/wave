# Manual test checklist - the parts automated tests can't touch

2407 automated tests (`python -m unittest discover -s tests`, plus 252 subtests) now cover the Python
logic across every phase and tier, but logic tests run against fakes - a fake `Shell`, a fake
`pyautogui`/`win32gui`, a fake `aec_audio_processing.AudioProcessor`, a stand-in PyQt6 with no real
window. None of that proves the real thing works on real hardware. This is the checklist for that
gap - mostly Tier 5 items, plus a few older ones that were already true before Tier 5 and just
hadn't been written down anywhere, plus a Tier 8 batch, plus Tier 9 updates below. Nothing here can
be fully automated away; each item still genuinely needs a human on a real OS, and (for the voice
rows) real audio hardware - **except** the Linux backend section below, where Tier 9 found that
this sandbox, despite having neither, could still stand up a real (if virtual) X11 desktop and a
real (if bus-less) `systemctl`, and ran the actual, unmodified backend code against both. That's
genuinely stronger evidence than a unit test against a fake `Shell`, which is why those specific
items are now checked off - but it's a simulation, not a human confirming it on their own physical
machine, so it's written up as exactly that, with precisely what's still open narrowed down rather
than claimed solved.

## Linux backend (`automation/backends/linux.py`)
Tier 9 update: everything below was, until now, "never run on a real Linux desktop" at all - true
right up to Tier 8. It's no longer true in the same way. This sandbox has no real X11 desktop or
systemd, but it turned out to have everything needed to build one: `Xvfb` (a real, virtual X
server - not a fake/stub), a real `openbox` window manager, real `wmctrl`/`xdotool` binaries
(installed via `apt-get`, which this sandbox's network allowlist permits), and a real `xterm` to
act as a target window. `LinuxBackend`'s actual methods were then run - unmodified, no fakes, no
mocked `Shell` - against that real (if virtual) desktop. That is genuinely stronger evidence than
"the command construction looks right" (what the unit tests check), but it is still not the same
claim as "a human confirmed this on their own physical Linux machine" - a VM/container desktop
under Xvfb+openbox is a reasonable stand-in for a real X11 session, not a proof that every real
desktop environment (GNOME/KDE/XFCE's own window managers, real multi-monitor setups, a real
Wayland compositor) behaves identically. The items below are rewritten to say exactly what was and
wasn't covered by that pass.
- [x] **Verified (Xvfb + openbox simulation, see above) - `close_window()`/`close_tab()` with a
      real DISPLAY set:** `pyautogui.hotkey('alt','f4')`/`('ctrl','w')` genuinely deliver a real X
      key event (confirmed via the lower-level equivalent, `xdotool key alt+F4`, since this
      sandbox's own Python 3.13 build has no `tkinter` and can't import the real `pyautogui`
      package itself - see the bug note below) and openbox's default keybindings really do close
      the focused window on Alt+F4, as most Linux window managers do by convention. Still open: a
      **real** desktop, in case some WM/distro rebinds or disables this; and `close_tab()`'s actual
      target (a browser tab) was never exercised, only the identical key-delivery mechanism.
  - 🐛 **Real bug found and fixed this tier:** `import pyautogui` itself calls `sys.exit(...)` (not
    a catchable exception) when its optional `mouseinfo` dependency can't find `tkinter` - which a
    real end-user Linux box has no guarantee of having installed. `SystemExit` isn't an `Exception`
    subclass, so the existing `except Exception` here never caught it; it would have taken down the
    *entire app*, not just failed this one action, the very first time anyone tried to close a
    window on such a machine. Confirmed by reproducing it for real (this sandbox's own Python 3.13
    has no `tkinter`) before fixing it. Now caught explicitly and turned into the same friendly
    `NotSupported` every other missing-tool case in this file already gets; regression-tested in
    `tests/test_phase4_platform.py`.
- [ ] On a headless box or a bare TTY with no X server (DISPLAY unset): this specific case - no
      crash, no raw `KeyError: 'DISPLAY'` - was already re-confirmed for real this tier (genuinely
      unset `DISPLAY`, not mocked), so this item is really just asking a human to double check it
      once on their own box too.
- [ ] Every other `linux.py` method (`launch_app`, `toggle_radio`, `open_system_setting`, ...) not
      covered above - all of `linux.py` is still marked experimental in `docs/PLATFORM.md`, and
      none of these happened to be exercisable through the same Xvfb setup (they talk to
      NetworkManager/rfkill/gnome-control-center, not a window manager).
- [x] **Verified (real, unmocked `systemctl`, see above) - Tier 8 `start_service`/`stop_service`:**
      this sandbox's container has the `systemctl` binary but no real systemd/D-Bus behind it
      (`Failed to connect to bus: Host is down` - itself a real, somewhat common case: minimal
      containers and some WSL setups are systemd-less Linux too). Running the real, unmocked
      backend method against that confirmed it fails exactly as intended - a plain "couldn't
      start/stop" reply, no crash, no hang - even though the SPECIFIC failure reason (no bus) isn't
      one of the two the message names ("may not exist, or may need elevated privileges"). That
      wording is still accurate (it doesn't claim something false), just not maximally specific for
      this one case; leaving it as-is rather than changing user-facing copy on a judgment call like
      this without being asked. Still genuinely open: a real systemd box, both as a normal user
      (expect "access denied"-flavored failure) and as root (expect real start/stop success) against
      a service you're allowed to control.
- [x] **Verified (Xvfb + openbox simulation, see above) - Tier 8
      `list_open_windows`/`focus_window`/`minimize_window`/`snap_window`:** ran all four, unmodified,
      against two real `xterm` windows under a real EWMH window manager. All four worked correctly,
      confirmed by checking the actual effect, not just a success return code: `list_open_windows`
      returned both real titles; `focus_window` matched-and-raised one and correctly reported "not
      found" for a made-up name; `minimize_window` was confirmed via the window's real
      `_NET_WM_STATE_HIDDEN` property (via `xprop`), not just wmctrl's exit code; `snap_window`
      (left/right/maximize, plus the invalid-position guard) was confirmed via each window's real
      on-screen geometry (via `xdotool getwindowgeometry`) ending up in the correct half/all of the
      1280x1024 screen (off by a couple of px from an exact half - openbox's own title-bar/border
      decoration, not a bug). Still genuinely open, and a real, known gap, not a hedge: a **native
      Wayland** session (no Xwayland) - `wmctrl`/`xdotool` are fundamentally X11 tools and are
      expected to either silently do nothing or fail outright there, which this sandbox (X11-only)
      can't exercise either way; and a real desktop's own window manager (GNOME/KDE/XFCE etc. may
      differ from openbox's defaults, especially around window decoration offsets).

## macOS backend (`automation/backends/macos.py`) - never run on a real Mac
- [ ] `close_window()`/`close_tab()` with Accessibility permission **granted**: `cmd+w` actually fires.
- [ ] Same, with Accessibility permission **denied**: confirm a friendly `NotSupported` message, not a
      raw crash - this is the one failure mode the Tier 5 fix can't fully pre-verify in CI, since there's
      no environment variable to flip the way `DISPLAY` works on Linux; only a real Mac with the
      permission actually turned off proves the try/except around `pyautogui.hotkey()` catches
      whatever macOS/pyautogui actually raise there.
- [ ] Everything else in `macos.py`, same as the Linux row above.
- [ ] **Tier 8, `start_service`/`stop_service`:** confirm `launchctl start`/`stop` actually works against a
      REAL launchd label (`com.apple.something`) on a real Mac - this was reasoned through from
      launchctl's documented legacy subcommands, never executed against a real `launchd`.
- [ ] **Tier 8, `list_open_windows`/`focus_window`/`minimize_window`/`snap_window`:** the highest-priority
      new item on this whole list - these are hand-written AppleScript (`osascript -e "..."` talking to
      `System Events`) that has **never been run against a real `System Events`**, unlike every other
      macOS method in this file, which at least runs real `pyautogui`/`subprocess` calls even if those
      calls have never been checked on a real Mac. Confirm: Accessibility permission granted vs.
      denied (same two-sided check as `close_window` above); that `(name of w) contains "..."` actually
      matches real window titles; that `set position of w to {...}`/`set size of w to {...}` actually move
      and resize a real window without a syntax error; and that the `Finder`-bounds-of-desktop screen-size
      trick returns sane numbers on a real display (including a multi-monitor setup, which it was never
      designed with in mind).

## Windows backend - Tier 8 additions only (Windows is otherwise the one fully-supported, real-hardware-verified platform)
- [ ] `start_service`/`stop_service`: `sc start`/`sc stop` against a real Windows service, both as a
      normal user (expect a plain "access denied"-flavored reply for anything privileged) and as
      Administrator (expect it to actually start/stop). Confirm the critical-service refusal list
      (`RpcSs`, `DcomLaunch`, `EventLog`, ...) genuinely can't be stopped even as Administrator - the
      refusal happens in `app_control.py` before the backend is ever called, so this should hold
      regardless of privilege.
- [ ] `list_open_windows`/`focus_window`/`minimize_window`: real `win32gui.EnumWindows` against
      whatever's actually open - these were only ever run against a hand-written fake standing in for
      `win32gui`/`win32con` (pywin32 isn't installed in this sandbox at all), so this is the first time
      the real enumeration/restore/foreground sequence runs against the real Windows window manager.
- [ ] `snap_window`: confirm the `win+Left`/`win+Right`/`win+Up` combo actually snaps the target window
      (not whatever else happens to have focus) - there's a `SetForegroundWindow()` + a short sleep
      before the hotkey fires specifically to avoid this race, but it's only ever been checked by
      reading the code, never by watching a real screen.

## Phase 5 - voice quality (needs a real mic + speakers, not just fakes)
- [ ] `AEC_ENABLED=true` with Wave talking and the mic open at the same time: confirm Wave's own voice
      doesn't get picked back up as a (near-)wake-word or a command. Compare against
      `AEC_ENABLED=false` to confirm the difference is real, not placebo.
- [ ] Tune `AEC_STREAM_DELAY_MS` against your actual mic/speaker round-trip if echo is still audible -
      the default (80ms) is a guess, not a measurement.
- [ ] Force each TTS fallback tier in turn (e.g. temporarily rename/uninstall `sounddevice`, then
      `pygame`, then unset `TTS_LOCAL_MODEL_PATH`) and confirm Wave actually falls through to the next
      one instead of going silent, and that `last_playback_ok` / the UI's failure surface
      (`on_speech_finished`) only ever reports `False` when *every* tier genuinely failed.

## Phase 6 - proactive nudges (needs real time passing, not mocked clocks)
- [ ] Let the battery actually drain to `PROACTIVE_BATTERY_THRESHOLD`% unplugged: confirm one nudge
      fires, not zero and not a repeat every `PROACTIVE_CHECK_INTERVAL`s.
- [ ] Plug in, unplug, drop below the threshold again: confirm it re-arms and nudges a second time.
- [ ] Put a real event `PROACTIVE_CALENDAR_LEAD_MINUTES` out on the calendar and confirm the nudge
      timing feels right, not just unit-test-correct.

## Tier 5 item 14 - config validation, at real startup
- [ ] Put a genuinely malformed value in `.env` (e.g. `TOOL_LOOP_MAX_STEPS=banana`) and launch Wave for
      real: confirm it starts normally (doesn't crash) AND that the warning actually lands in
      `wave_system.log`, not just in a test's captured logger output.

## Tier 5 item 17 - WorkflowsPanel, by hand
- [ ] Actual mouse drag-to-reorder in the steps list (`QListWidget.InternalMove`) - the automated
      tests drive `_move_step()` directly (the ↑/↓ buttons' code path) since a stand-in with no real
      window can't simulate a drag; dragging exercises Qt's own `rowsMoved` signal instead, which
      `_mark_dirty()` is wired to separately.
- [ ] Double-click a step to edit it, and the various save/rename/delete dialogs end to end - the
      automated tests mock `QInputDialog`/`QMessageBox` to control exactly what they "return" rather
      than actually popping a dialog.

## Tier 10 - sleep commands, by voice
- [ ] Awake, say "unmute", "mute kar do" and "PC ko sleep karo": Wave should act on each (system
      unmute/mute; the last one is a real request the brain answers) and stay awake. Before Tier 10
      all three silently put Wave to sleep.
- [ ] Say "so jao", "stop listening" and "Hey Wave, go to sleep": each should put Wave to sleep.
- [ ] Say a long command (10-14 seconds): it should no longer be cut off at 8 seconds.

## Tier 11 - the voice loop core (needs a real mic + speakers)
Install `requirements-voice-local.txt` first; Settings → Voice → *Wake Word Detection* should then
read "On-device 'Hey Wave' model ready" (it loads the first time Wave goes to sleep).
- [ ] **False wakes - the one-hour TV test.** Wave asleep, a TV/YouTube talk show or news channel
      playing at normal volume for an hour, nobody saying "Hey Wave". Count wakes (each one shows a
      "LISTENING" badge and logs `Wake word detected` in `wave_system.log`). Target: **≤ 1 per
      hour**. If it's more, raise the threshold (Settings → Wake Sensitivity, or
      `WAKE_WORD_THRESHOLD=` in `.env`) and repeat. Write the number and threshold down here.
- [ ] **False rejects.** Say "Hey Wave" 40 times across a day - near and across the room, quietly
      and normally, with music on and off. Target: **≥ 38 of 40** wake Wave (≤ 5% missed).
      Include the way you naturally say it (Indian-English "Hey Vave" counts).
- [ ] While asleep, talk normally near the mic for a minute without the wake word: with the
      on-device model nothing should be transcribed (no `Recognized (...)` lines in the log).
- [ ] **Wake + command in one breath:** "Hey Wave, Chrome kholo" opens Chrome with no "Systems
      active." in between; plain "Hey Wave" still answers "Systems active.".
- [ ] **Barge-in gate:** ask a long question, and while Wave answers, have someone else talk (or
      play a video) without saying "Wave": Wave keeps talking. Then say "stop" / "ruk jao" /
      "रुको": it stops at once and the bubble ends with "⏹ Stopped".
- [ ] "New question: what time is it" said over a long answer cuts it off and answers the time.
- [ ] Ask Wave to delete a file (it asks to confirm), then say "never mind": a later "yes" must
      NOT delete it (the pending confirmation was dropped).
- [ ] **Streaming:** a long LLM answer appears word by word and speech starts after the first
      sentence, with no noticeable pause between sentences (Tier 2's version paused for a network
      round-trip before every sentence).
- [ ] A reply with a code block: the bubble shows the code; the voice says "Code block omitted".
- [ ] "Send hi to mom on WhatsApp" and "close chrome" now ASK before acting (the UI's old keyword
      shortcuts sent/closed without asking).
- [ ] The waveform moves with your voice (and is flat-ish in a quiet room), not random bars.
- [ ] **TTFA:** run the 20-utterance protocol in `docs/TTFA_PROTOCOL.md`, then
      `python -m telemetry.ttfa_report --markdown`, and paste the table into that file. Targets:
      fast path p50 ≤ 0.8 s, LLM p50 ≤ 2.0 s.

## Tier 12 - the capture pipeline (needs a real mic + speakers)
`wave_system.log` should say "Continuous mic stream started (Tier 12 capture pipeline)" shortly
after the mic turns on. If it says "using speech_recognition's listen() instead", the reason is
on the line before (no `sounddevice`, or the device wouldn't open) and everything below falls
back to Tier 11 behaviour.
- [ ] **A 30-second command** ("remind me tomorrow at nine to ..." with a long description, pausing
      briefly to think once or twice): one bubble, nothing cut off.
- [ ] **Short commands still end quickly** - "volume up" is answered without a noticeable wait
      after you stop talking (end-of-speech is now 0.45 s of silence, exact to 20 ms).
- [ ] **Unplug the microphone** (USB/headset) while Wave is awake, wait 5 s, plug it back in:
      Wave hears you again without a restart (log: "Mic stream reopened").
- [ ] **Plug headphones in** mid-session: the log says "Default microphone changed (...)" and
      capture continues from the new device.
- [ ] **A fan or AC switched on** near the mic: after a few seconds Wave is not stuck "listening"
      to it (no endless phrase; nothing transcribed).
- [ ] **AEC on vs off**: on laptop speakers, ask a long question and talk over the answer with
      "Wave, stop" - with `AEC_ENABLED=true` (and `aec-audio-processing` + `sounddevice`
      installed) Wave should hear you reliably; with `AEC_ENABLED=false` noticeably less so.
      Also with AEC on: Wave's own voice must not trigger commands. Note what you hear here.
- [ ] **Live captions** (local Whisper installed, 4+ CPU cores): while you speak a long sentence,
      a faint bubble shows the words so far; it's replaced by the real message when you stop.
- [ ] **Whisper hygiene**: sit quietly for a minute with the mic on, cough once, tap the desk -
      no "Thank you." / "Thanks for watching!" commands appear.
- [ ] **Loud barge-in**: while Wave answers on speakers, say loudly and clearly "what about
      tomorrow" (no "Wave"): it stops and answers that. A TV at normal volume across the room
      must NOT do the same. (Needs real echo cancellation; `BARGE_IN_SNR_DB=0` turns it off.)
- [ ] `py scripts\windows_smoke_check.py --mic` reports the rate the mic opened at, level
      updates > 0 and a plausible noise floor (about -70 to -40 dBFS in a quiet room).
- [ ] **No gaps between sentences** of a long spoken reply (one output stream now).

## Tier 13 - bilingual voice + local neural TTS (needs a real mic + speakers)
- [ ] **The 60-utterance bilingual set.** Speak 20 clearly-English, 20 clearly-Hindi
      (Devanagari-script thinking, not transliterated in your head) and 20 genuinely-mixed
      Hinglish utterances - a realistic mix of short and long, the kind you'd actually say to
      an assistant, not a word list. Target: **recognized acceptably** (the right intent fires,
      or the transcript is close enough that a human reading it would understand what you
      meant) for at least 54 of 60 (90%). Note the count and which utterances missed here.
- [ ] **Hindi replies are spoken in a Hindi voice with no setting change.** Ask a question in
      Hindi or Hinglish that gets a Hindi-language reply (e.g. "mausam kaisa hai" with a Hindi
      locale configured) and confirm it's read in the Hindi voice (`voice_hi`,
      `hi-IN-SwaraNeural` by default), not the English one.
- [ ] **A reply that code-switches mid-answer** (ask something that naturally gets an answer
      mixing English and Hindi/Hinglish, or just check with a long explanation) is spoken with
      the right voice *per sentence* - listen for an English sentence followed by a Hindi one,
      or vice versa, each in the correct voice, not one voice for the whole reply.
- [ ] **A reply with numbers, a time, a date, or a rupee amount** is actually spoken as words
      ("twenty-five" / "पच्चीस", "nine thirty", "one lakh fifty thousand rupees"), not read as
      raw digits - try this in both an English and a Hindi reply.
- [ ] **Piper offline fallback** (`pip install -r requirements-voice-local.txt`,
      `python -m piper.download_voices en_US-lessac-medium`, point `TTS_LOCAL_MODEL_PATH` at
      it, then disconnect the network): an English reply is still spoken, in Piper's voice -
      noticeably more natural than the old SAPI voice. Note what you hear.
- [ ] **Piper auto-download** (same as above but `TTS_LOCAL_AUTO_DOWNLOAD=true` and
      `TTS_LOCAL_MODEL_PATH` left blank instead of downloading by hand): the voice downloads
      itself on first use (watch `wave_system.log` for "Downloaded and verified Piper voice"),
      and works exactly the same afterward.
- [ ] **Kokoro, if you opt in** (`TTS_KOKORO_ENABLED=true`, `TTS_KOKORO_AUTO_DOWNLOAD=true`,
      network on for the ~120MB first-time download, then disconnect it and also disable
      Piper/remove its model to force this path): a Hindi reply is still spoken, in an actual
      Hindi voice - compare how it sounds and how long it takes to start speaking against
      Piper's English voice on an equivalent-length English reply. Note both here; this
      project's own sandbox measured Kokoro at roughly 2x real time (`docs/VOICE_LOCAL.md`) -
      confirm whether your real hardware does meaningfully better.
- [ ] **Both local engines declined/absent still falls back to SAPI** (uninstall/don't install
      `piper-tts`/`kokoro-onnx`, or leave both auto-download flags off with no model path set,
      then disconnect the network): Wave still speaks - the old, more robotic voice - instead
      of going silent.

## Tier 14 - system control surface I (needs real hardware; everything below was only ever
checked against a fake backend, by construction - a unit test can't verify what a real OS
actually does)
- [ ] **Shutdown/restart, for real**, with a short delay: confirm the PC genuinely shuts down /
      restarts at roughly the stated time, not immediately and not never. Then do it again and
      say "cancel shutdown"/"cancel restart" before it fires: confirm it's genuinely cancelled
      (the PC stays on), not just that Wave *says* it cancelled something.
- [ ] **Sleep, hibernate, log off** - each actually puts the PC into that state. Schedule one with
      a delay and cancel it before it fires (Wave's own `threading.Timer`, not a native OS
      mechanism for these three) - confirm the action genuinely never happens.
- [ ] **Volume**: set an exact %, mute, then say "unmute" - confirm it actually unmutes (this is
      the specific bug the roadmap called out: before this tier, "unmute" while already unmuted
      used to mute instead). Volume up/down with no real backend reachable should still nudge
      volume via the OS media keys (`pyautogui`) as a fallback - unplug/disable whatever the real
      backend needs, if you can, to see this path.
- [ ] **Brightness**: set an exact %, nudge up/down, ask for brightness status - confirm the
      screen genuinely changes (laptop panel, not an external monitor without DDC/CI support,
      which `screen_brightness_control` often can't reach).
- [ ] **Windows**: move and resize a specific window (not just whichever has focus), maximize,
      restore, toggle always-on-top, fullscreen, minimize all / show desktop, switch to a named
      window, and `restart_app` on something you have open - confirm each does what it says on
      your real desktop, and that undoing a move/resize/always-on-top actually puts it back.
- [ ] **GPU status**, with `pip install nvidia-ml-py` on a machine with a real NVIDIA GPU: confirm
      it reports genuine utilization/temperature, not just that it doesn't crash. On a machine
      without one (or without the package), confirm the honest "needs the optional package" /
      "no NVIDIA GPU" message, not a stack trace.
- [ ] **CPU temperature**: on Linux with real `coretemp`/`k10temp` sensors, confirm a real number
      comes back. On Windows/macOS, confirm the honest "isn't available on this system" message -
      this is a documented platform gap (`psutil` has no sensor API there), not something to try
      to make succeed.
- [ ] **Top processes**, by CPU and by memory: confirm the list actually matches Task
      Manager/`top`/Activity Monitor at that moment, ordering included.
- [ ] **`list_services`**: confirm the names returned are real running services
      (`services.msc`/`systemctl list-units`/`launchctl list`), and that the count in the reply
      matches what's actually running.
- [ ] **Disk usage**: confirm every real mounted drive/partition shows up with believable
      numbers, and that a tiny/EFI partition and anything unreadable (an empty optical drive) are
      silently skipped rather than shown wrong or crashing the whole reply.
- [ ] **Battery status**, on a real laptop: charge %, plugged-in vs on-battery, and a plausible
      time-remaining while unplugged. On Windows specifically, confirm a battery *wear* percentage
      appears too (`powercfg /batteryreport` needs to actually run on your machine) - and that a
      desktop/VM with no battery gets the plain "likely a desktop, or a VM" message instead of an
      error.

## Tier 15 - system control surface II (needs real hardware; everything below was only ever
checked against a fake backend/fake filesystem, by construction - a unit test can't verify what
a real OS, a real app with focus, or a real browser actually does)
- [ ] **Keyboard chords on a real app**: open a text editor and try `press_key` with a few plain
      chords (`"ctrl+s"`, `"ctrl+shift+t"`, a single key like `"enter"`) - confirm the chord
      actually reaches the focused app, not just that Wave says it sent something.
- [ ] **The chord blocklist**: ask for `win+r` (or `super+r`) and `ctrl+alt+del` - confirm each is
      refused outright (no Run dialog, no secure-desktop screen), not merely asked to confirm.
- [ ] **The `alt+f4`/`cmd+w` system-window heuristic**: open Task Manager (or Activity Monitor, or
      a terminal) and ask Wave to close it with `press_key` - confirm it's refused. Then switch focus
      to an ordinary app (a text editor, a browser tab) and ask again - confirm it goes through.
      Also try it with an unusual/unreadable window title if you can manufacture one - confirm
      Wave doesn't block it just because it couldn't read the title (the "can't tell → don't
      block" rule is honest uncertainty, not a reason to over-refuse).
- [ ] **Browser hotkeys, in a real browser**: `new_tab`, `next_tab`/`previous_tab` (confirm these
      cycle tabs, not switch apps - they deliberately send the browser's own `ctrl+tab`, never
      `cmd+tab`, even on macOS), `reload_page`, `zoom_in`/`zoom_out`/`zoom_reset`, and all four
      scroll commands - confirm each does the expected thing in the actual foreground tab.
- [ ] **Clipboard write**, with `pyperclip` actually installed (it's already in
      `requirements.txt`, but confirm it's genuinely present - the sandbox this was built in
      couldn't install it): `copy_text`/`paste_text`/`select_all_text`, and ask Wave to type
      something containing a password-like string - confirm the Activity timeline shows only a
      length, never the actual text.
- [ ] **Undo vs. redo keystroke**: in a real text editor, type something, say "undo" (Wave's own
      `undo_last_action` - should be a no-op here, nothing to undo on Wave's side) vs. "press
      undo"/"ctrl z" (`undo_keystroke` - should send Ctrl+Z to the editor and actually undo the
      typing) - confirm these two stay genuinely distinct, not interchangeable.
- [ ] **Every folder shortcut, on each OS you have**: Downloads/Documents/Pictures/Music/Videos,
      This PC, Recycle Bin, Task Manager, Control Panel - confirm each opens the real thing.
      Specifically on **macOS** (never run against a real `Finder`/`System Events` before this
      tier): confirm the `osascript`-driven `this_pc`/`recycle_bin`/`task_manager`/`control_panel`
      calls actually work, and that `control_panel` finds "System Settings" on Ventura+ or falls
      back to "System Preferences" correctly on your actual macOS version. On **Linux**: confirm
      `this_pc` honestly opens your home folder (there's no single "This PC" view) and
      `recycle_bin` opens/creates the real `~/.local/share/Trash/files`.
- [ ] **`empty_recycle_bin`, for real** - put a few disposable test files in the recycle bin/trash
      first (this is genuinely irreversible, on every OS). Confirm it actually empties, and that
      the confirmation gate fires beforehand exactly like any other T3 action.
- [ ] **Download watcher, with a real browser download**: say "watch for my download" (or
      equivalent), then actually download a file - confirm Wave notices completion. Try it once
      with a slow/large download that shows a `.crdownload`/`.part` file mid-flight, and once with
      a download that finishes fast enough that the temp file is never observed, to exercise both
      detection signals. Then try `cancel_download_watch` mid-watch, and separately let a watch run
      past its timeout with nothing downloaded - confirm the honest timeout message, not a hang.

## Tier 16 - productivity surface (needs real network access and/or `dateparser` installed; the
sandbox this was built in can't resolve any real hostname at all - see `web/web_ops.py`'s
`validate_url()` docstring - so everything below was only ever verified against mocked responses)
- [ ] **`summarize_page`/`fetch_page_text` against a real URL**: ask Wave to summarize an actual
      web page (a news article, a Wikipedia page). Confirm it fetches real content, strips
      script/style/nav chrome sensibly, and that the LLM's summary actually reflects the page -
      not just that `fetch_page_text` returns *some* non-empty string. Also try a URL that 404s,
      one that points at a PDF or image (non-HTML content type), and a genuinely huge page (to see
      the real truncation message land somewhere reasonable, not mid-word in a way that reads as
      broken).
- [ ] **`play_on_youtube`'s yt-dlp path, for real**: ask Wave to play something on YouTube with
      `yt-dlp` genuinely installed and network access working - confirm it actually opens the
      right video (yt-dlp's search result), not a wrong/unrelated one, and that the browser opens
      promptly rather than hanging on the metadata-only lookup. Then try it with `yt-dlp`
      uninstalled (or temporarily broken) to confirm the `pywhatkit` fallback still works exactly
      as it did before this tier.
- [ ] **`play_on_spotify`, for real**: confirm it actually opens a Spotify search in the browser
      (or the desktop app, if that's how `open_website`'s Spotify URL resolves on your machine) for
      a real query, not just that Wave claims to have opened something.
- [ ] **Every mode bundle, on real volume/mute/brightness hardware**: `study`/`gaming`/`focus`/
      `dnd`/`sleep` - confirm each one's actual bundle of settings lands correctly (e.g. `dnd`/
      `sleep` genuinely mutes and dims), and that `disable_mode` restores your *actual* prior
      volume/mute/brightness, not just plausible-looking values. If your hardware refuses one
      setting in a bundle (e.g. brightness unsupported on an external monitor), confirm the
      rollback behavior genuinely reverts whatever else in that bundle already changed, on a real
      backend rather than the test suite's fake one.
- [ ] **`add_todo`'s due-date-to-reminder linkage, with `dateparser` actually installed**: add a
      to-do with a due date/time ("remind me to call the dentist tomorrow at 5pm") and confirm both
      the to-do *and* a real reminder get created, with the reminder firing at the time you'd
      actually expect from natural-language phrasing - this tier's own test suite stubs out
      `_set_reminder` specifically because `dateparser` isn't installed in the build sandbox, so
      the real NLP parsing here has never actually run end to end.

## Tier 18 - screen vision, calendar sync, time-triggered workflows, proactive engine depth (needs
a real vision-capable LLM provider, a real Google account, and/or a real external calendar feed;
everything below was only ever verified against mocked HTTP responses, a fake OAuth flow, and a
frozen/mocked clock, by construction)
- [ ] **`describe_screen` against a real vision-capable provider**: with an actual error dialog,
      chart, or photo on screen, ask Wave "what's on my screen?" and confirm the description is
      genuinely useful and accurate against the real provider's real vision model - not just that
      some string came back (every test here mocks the LLM response). Then point Wave at a
      text-only/non-vision model (or temporarily break the vision call) and confirm it falls back
      to the existing Tesseract OCR path with a sensible result, not a stack trace.
- [ ] **Google Calendar sync, with a real account**: create `credentials.json` yourself (Wave
      ships none, on purpose - see `automation/calendar_sync.py`'s own docstring), run
      `setup_google_calendar`, and actually click through the real browser OAuth consent screen
      `InstalledAppFlow.run_local_server()` opens - this has never run end to end in the build
      sandbox (no browser, no real Google account there). Confirm a real event on your real
      calendar shows up after `sync_calendars`; confirm a recurring Google event comes back as
      every real occurrence (Google's API expands these server-side - confirm it actually does,
      not just that one instance appears); and confirm `remove_calendar_feed("google")` genuinely
      revokes local access by deleting the cached token file, without you having to go find it
      yourself.
- [ ] **ICS feed sync, against a real external calendar**: point `add_ics_calendar_feed` at a real
      `.ics` URL (a shared Google/Outlook/Apple calendar's public feed works) and run
      `sync_calendars` - confirm real events land with the right local time (this exercises the
      hand-rolled RFC 5545 parser's UTC/`TZID`/floating-time handling against whatever a real
      calendar app actually emits, not just this tier's own hand-built fixtures), and that a
      recurring event you can see in the source calendar comes back labelled `daily`/`weekly`, or,
      for anything more complex (custom intervals, `UNTIL`, `COUNT`, `BYDAY`), honestly falls back
      to a one-off first-occurrence import rather than silently guessing the rest.
- [ ] **Time-triggered workflows, across a real clock and a real restart**: schedule a workflow a
      few minutes out ("run my morning routine in 3 minutes"), leave Wave running, and confirm it
      actually fires unprompted - not just that `list_workflow_schedules` says it will. Then
      schedule one, fully quit and restart Wave before it's due, and confirm it still fires on
      time (the schedule is persisted in the database, not just held in memory). Try a
      `daily`/`weekly` repeat too and confirm it reschedules itself for the next occurrence
      instead of running once and going quiet.
- [ ] **Proactive triggers, over real elapsed time**: this tier's own tests all use a
      frozen/mocked clock, so none of the following has ever been observed actually happening in
      real time. Leave Wave running for a real session and confirm: a morning-briefing prefetch
      actually appears around your configured time; stepping away from the keyboard for a while
      produces an idle/break nudge, and coming back resets it; doing the same action at the same
      hour on 3+ real days produces a "turn this into a routine?" habit nudge; a streak milestone
      nudges when it's actually reached; and a low-disk nudge fires once on a real low-disk device
      without repeating on every check while you're still below the threshold, then fires again if
      the free space flaps back above and below it past the cooldown window. Also confirm the
      DND/focus gate actually silences all of these for real when a focus mode is on, and that
      turning any single `PROACTIVE_*_ENABLED` flag off in `.env` silences only that one trigger.

## Tier 19 - robustness: startup health check and Ollama lifecycle manager (needs real audio
hardware and/or a real Ollama install; everything below was only ever verified against mocked/
stubbed sounddevice, pygame, speech_recognition and HTTP responses, by construction - cancellation,
timeouts, the thread supervisor, the background DB writer, and log correlation IDs are all pure
in-process logic with no real-hardware dependency and are NOT listed here, since this sandbox's own
automated tests already exercise them for real, not just against a mock)
- [ ] **`wave_start.pyw --healthcheck`, on your real machine**: run it from a terminal with your
      actual mic/speaker connected, your actual `.env` LLM provider configured, and Tesseract
      either installed or not. Confirm the microphone and speaker checks report OK when the
      hardware genuinely works, and confirm unplugging/disabling one of them (or picking a bogus
      input/output device in Windows' sound settings) makes that specific check report WARN/
      PROBLEM instead of silently staying OK - this tier's own tests mock `sounddevice`/
      `speech_recognition` entirely, so a check that always reports OK regardless of the real
      device state would pass every automated test here and still be useless in the field.
      Confirm the exit code is genuinely 0 when everything critical is fine and 1 when something
      critical (LLM provider unreachable, no voice stack at all) is broken - a script/CI job
      depends on that exit code, not just the printed text.
- [ ] **Ollama lifecycle manager, with a real Ollama install**: set `LLM_PROVIDER=ollama` (or leave
      the Groq-fallback path on and stop Groq from resolving) with Ollama genuinely installed but
      **not already running**, then start Wave and confirm it actually launches `ollama serve`
      itself (check `ollama ps`/Task Manager for a new process) rather than just timing out waiting
      for one that was never started. Confirm a model you haven't pulled yet gets pulled
      automatically (watch it progress, confirm Wave's own UI/startup isn't blocked while a
      multi-GB download runs) and that a model already present is *not* re-downloaded. Quit Wave
      and confirm it stops the `ollama serve` process it started - then start Ollama yourself
      first (so Wave finds one already running), quit Wave, and confirm that process is still
      running afterward (the lifecycle manager must never stop a server it didn't start itself).
      Finally, set Ollama out of the picture entirely (`OLLAMA_AUTOSTART=false` or no
      Ollama-related config at all with a non-Ollama provider) and confirm none of this runs at
      all - no `ollama serve` attempt, no delay at startup.

## Tier 20 - trust & memory (almost entirely pure logic - no real-hardware dependency for most of
it; secret redaction, the security event log, the exfiltration/speak/RAG guards, cross-turn chunk
joining, provenance tracking, "why did you do/notify" and the Unicode target normalization are all
fully exercised directly in this sandbox's own automated tests and are NOT listed here. The one
genuine real-dependency gap is below.)
- [ ] **Memory semantic supersede, with real embeddings installed**: this build sandbox has no
      `sentence-transformers` installed, so `database/db_manager.py`'s `save_memory()` always takes
      the "no embeddings available" path here (verified honestly as its own test case, not hidden),
      and the actual supersede logic (an existing, semantically-same ≥ 0.92 key gets updated in
      place instead of a near-duplicate being inserted) is only ever tested with
      `database.embeddings.embed()`/`db.semantic_recall()` mocked at the exact boundary
      `save_memory()` calls through. On a real install with `pip install sentence-transformers`
      (or however this project's optional voice/embeddings extras get installed), say
      "remember my favorite color is blue", then later say "remember my favourite colour is blue"
      (different spelling, same meaning) and confirm `recall_memory` / `why_remembered` show ONE
      fact, not two - and that the original key (not a new one) is what survived.

## Tier 21 - UI depth (almost entirely new UI surfacing already-tested logic - the six management
pages, design tokens, the live activity banner, and local-only usage stats are all fully exercised
directly in this sandbox's own automated tests and are NOT listed here, since none of them depend
on real hardware. What's below is genuinely new real-device/real-network surface, plus a look check
this sandbox has no display for.)
- [ ] **Theme roster, by eye.** All 11 themes (5 original + Slate Dark, Violet Dark, Daylight, Paper
      Light, Ocean Light, Mint Light) were only checked for internal token contrast (dark text on
      light surfaces) in this sandbox, never a rendered screenshot. Cycle through all 11 in
      Settings > Appearance and confirm every page - including the 6 new management pages and the
      setup wizard - is actually legible in each one, light themes included.
- [ ] **Real mic/speaker picker + level test** (setup wizard, Microphone step). `voice/audio_devices.py`'s
      device enumeration, level measurement, and tone playback are only exercised against mocked
      `pyaudio`/`speech_recognition` here (no real audio hardware in this build sandbox). On a real
      machine: confirm the input/output combos list your actual devices, "Test Microphone" shows a
      sensible level while you talk and flags silence when you don't, and "Play Test Sound" actually
      plays an audible tone through the selected output device.
- [ ] **Wake-word calibration** (setup wizard, new step). `voice/stt.py`'s `recalibrate()` is unit-tested
      against a mocked `sr.Microphone`; on real hardware, click "Calibrate Now" in a quiet room and
      confirm the reported ambient level/threshold numbers look sane (not all zeros) and that wake-word
      detection sensitivity actually feels different afterward.
- [ ] **Local-voice (Piper) download** (setup wizard / Settings > Voice). With `pip install piper-tts`
      actually installed and the checkbox turned on, confirm WAVE really downloads a voice model on
      first use (not just that the config flag flips) and that offline/local speech then genuinely
      sounds different from the cloud TTS voice.
- [ ] **Calendar connect** (setup wizard, optional step). Enter a real `.ics` feed URL and confirm
      `sync_calendars` actually pulls its events in - the wizard step itself just calls
      `CalendarSync.add_ics_feed()`, already covered elsewhere; this is about a real external feed,
      not the plumbing.

## Tier 22 - docs, packaging, installer, real-hardware release pass (every item below needs a real
Windows machine; none of it can be built, compiled, or run from this sandbox - see
`docs/INSTALLER.md` for the full explanation of why)
- [ ] **Build the `.exe` fresh from `requirements.lock`.** `pip install -r requirements.lock` then
      `.\build_exe.bat` (`docs/PACKAGING.md`). Confirm the build succeeds and `dist\WaveAI.exe`
      launches, before trusting the installer step below to wrap a working binary.
- [ ] **Compile the installer.** `iscc installer\wave_installer.iss` (needs Inno Setup 6 on
      `PATH`). Confirm it compiles clean with no Pascal Script errors - this script has never been
      run through `iscc` at all, only written against Inno's documented syntax.
- [ ] **Run the installer end to end.** Install → confirm the Start Menu shortcut, optional
      desktop icon, and optional "start at login" task all land correctly → let it launch Wave on
      finish → confirm the setup wizard opens exactly as it would from a bare `.exe`. Then
      uninstall, and confirm the program files are removed while anything under `config.DATA_DIR`
      (chat history, memories, reminders, notes) is left untouched.
- [ ] **SmartScreen/AV behavior, documented not panicked over.** The installer and `.exe` are both
      unsigned (`docs/INSTALLER.md`, `docs/PACKAGING.md`). Confirm what Windows actually shows on
      first run (likely a SmartScreen "unrecognized publisher" prompt) so that behavior is known
      and expected rather than discovered by surprise later.
- [ ] **Regenerate a true transitive `requirements.lock`.** Run `.\scripts\freeze_requirements.ps1`
      (documented in `requirements.lock`'s own header and `docs/INSTALLER.md`) inside a real venv,
      which installs every package - including the five Windows-only wheels this sandbox can't
      resolve at all (`pywin32`, `pycaw`, `comtypes`, `winsdk`, `keyboard`) - and overwrites the
      lock's reference-only transitive section with a real Windows `pip freeze`. Confirm the
      regenerated file still installs cleanly and Wave still runs from it.
- [ ] **Re-run the full test suite and bench on the real machine**, as a final sanity check that
      nothing about a real Windows filesystem/registry/audio stack changes the result this
      sandbox already got: `cd tests && python3 -m pytest -q && python3 -m unittest discover`,
      then `python -m bench.run_bench --mode all --compare`.
- [ ] **Everything still open from earlier tiers.** This release pass is also the first real
      chance to clear the backlog of real-hardware items this project has been honestly carrying
      tier over tier - re-check the still-open boxes above (Tiers 11-21) while a real machine is
      in hand, rather than only the Tier 22 items freshly added here.

## Tier 23 - remote mobile dashboard (post-roadmap; needs your PC and a phone on the same Wi-Fi -
none of this can be checked from a headless sandbox, only pairing/session/CSRF *logic* was tested
in `tests/test_tier23_remote_dashboard.py`)
- [ ] **Enable the dashboard and pair a real phone.** Settings → Remote → tick "Enable remote
      dashboard" → confirm a QR code and an 8-character code both appear → on a phone connected to
      the *same* Wi-Fi, scan the QR code with its camera app and confirm it opens the pairing page
      pre-filled, or type the 8-character code by hand on `http://<the-shown-LAN-IP>:8787/login`.
      Confirm pairing succeeds and the phone lands on a working chat page.
- [ ] **Send a message from the phone and get a real reply.** Confirm it actually reaches Wave (not
      just a UI echo) - say something that triggers a real tool (e.g. "what time is it") and
      confirm the answer is correct, not a canned/placeholder string.
- [ ] **A T2/T3 action from the phone asks for confirmation exactly like the desktop would.** Ask
      for something that normally needs a spoken/typed "yes" on desktop (e.g. closing an app) from
      the phone, confirm the SAME confirmation wording comes back, and confirm replying "yes" from
      the phone actually carries the action out - not a silent no-op.
- [ ] **"Regenerate pairing code" actually signs the old phone out.** With a phone already paired,
      click "Regenerate pairing code" in Settings, then try sending another message from that same
      (now-unpaired) phone session - confirm it's rejected and sent back to the pairing page, not
      silently still working.
- [ ] **It's genuinely LAN-only.** From a device on a *different* network (e.g. phone on mobile
      data, Wi-Fi off), confirm the dashboard's URL simply doesn't load - no router port-forwarding
      was set up, and none should be needed for this to work on the same Wi-Fi.
- [ ] **Turning it off actually stops the server.** Untick "Enable remote dashboard" (or close
      Wave entirely), then confirm the dashboard's URL stops responding on a device that was
      previously paired.
- [ ] **How the Remote settings page actually looks.** The QR image, the warning text, the layout
      next to Security/Advanced in the sidebar - this sandbox has no display to check any of it
      rendered, only that the underlying widget logic runs.

## Tier 24 - a second UI technology, built with SARA's own web stack (post-roadmap; needs your PC -
no display and no real pywebview/WebView2/GTK-WebKit backend in this sandbox, so only the bridge
logic was tested, never the rendered page)
- [ ] **Launch the new web UI and confirm it opens.** Double-click (or run) `wave_web_start.pyw` -
      confirm a window opens showing the chat page, with no error dialog, and that
      `wave_start.pyw` (the original PyQt6 UI) still opens and works completely normally too -
      neither should affect the other.
- [ ] **Chat actually works end to end.** Send a message in the web UI's chat page, confirm a
      real streamed reply comes back (not a placeholder), and that a tool-triggering message
      (e.g. "what time is it") gives the correct answer.
- [ ] **Voice works through the new worker path.** Toggle the mic on from the web UI, say "Hey
      Wave" (or your configured wake phrase), confirm it wakes, transcribes, and replies - this
      exercises `webui/voice_worker.py`'s `threading.Thread`-based ports of the original Qt voice
      workers for the first time against real microphone/speaker hardware.
- [ ] **A T2/T3 action from the web UI asks for confirmation exactly like the desktop PyQt6 UI
      would.** Ask for something that needs a "yes" (e.g. closing an app) from the web chat,
      confirm the same confirmation wording appears, and that replying "yes" actually carries the
      action out.
- [ ] **Settings/data really are shared between both UIs.** Change a setting (e.g. a theme
      preference, or add a note) in one UI, then open the other UI and confirm the change is
      already there - both read/write the same `wave_settings.json` and the same database.
- [ ] **Every management page renders and works:** Memory, Notes & To-dos, Reminders, Calendar,
      Workflows, Skills, Performance, Security, Files, Clipboard, and the Remote-dashboard
      controls carried over unchanged from Tier 23 - confirm each one loads real data and its
      actions (add/edit/delete/run) actually take effect.
- [ ] **How the whole UI actually looks.** The glassmorphism panels, the design-token colors in
      both the dark and light themes, spacing, type, and motion/transitions - no display in this
      sandbox to check any of it rendered, only that the HTML/CSS/JS is syntactically sound and
      the bridge logic behind every page is tested.

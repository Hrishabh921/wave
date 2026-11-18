"""
automation/proactive.py — Phase 6: the proactive engine (Tier 3 gap #5).

Before this, Wave was purely reactive - it only ever spoke when asked
something. Reminders already fire for real (ui/main_window.py's QTimer-
driven ReminderWorker polls the DB every few seconds), but that's the user
explicitly asking to be told something later - nothing in brain/ or
automation/ was watching ambient signals (battery about to die, a meeting
starting soon) and deciding to say something unprompted. That's what this
adds.

ProactiveEngine is a plain-Python daemon thread with zero PyQt/Qt
dependency (same "automation/ stays Qt-free" layering as the rest of this
package) - ui/main_window.py bridges it into the GUI thread with a
pyqtSignal, the same add_listener(...)-callback-calls-.emit() pattern
already used there for automation.audit.audit and brain.skill_loader's
UserSkillLoader.

Ten triggers now, meeting the roadmap's own "2 -> >= 10" Tier 18 target -
the original two (battery low, upcoming event), a Tier 15 third
(downloads), and six Tier 18 additions:
  - battery: low percent + not charging, with a cooldown AND hysteresis so
    it nudges once, not every single check while sitting at 14% (re-arms
    once charging or back above threshold).
  - calendar: a stored event about to start, nudged once per event id
    (tracked in memory, not time-based, since an event only "starts soon"
    once per check run it's within the lead window).
  - downloads (Tier 15): unlike the two above, this one is OFF by default
    at the instance level - there's nothing to watch until watch_for_download
    arms it (arm_download_watch), and it self-disarms the moment it either
    sees a finished download or times out, rather than running forever.
    Folded into this SAME engine/thread rather than a second one, for the
    same reason the roadmap put it under "proactive" at all: it's one more
    ambient thing Wave notices and mentions unprompted, not a new kind of
    mechanism.
  - scheduled workflows (Tier 18): unlike the other nine, this one doesn't
    just NOTIFY - it actually runs the workflow's steps, closing the
    README's "workflows are on-demand only" limitation. Because it DOES
    something rather than just saying something, it's the reason
    check_once() below gates on DND/focus/sleep mode BEFORE any trigger
    runs - every trigger goes through that same gate, but this is the one
    that made it necessary. Due-ness is tracked in
    automation/workflow_scheduler.py's persisted workflow_schedules table,
    not RAM, since the roadmap's own "Done when" requires a schedule to
    survive a restart. Actually executing a workflow means calling into
    brain/ (WaveBrain._dispatch_tool), which this Qt-free, brain-free
    engine has no reference to and must not call directly from its own
    background thread - see set_workflow_runner() below for how the GUI
    side plugs that in safely.
  - reminder follow-ups (Tier 18): migrated from ui/workers.py's
    ReminderWorker - see _check_reminder_followups() for why only the
    follow-up half moved here, not due/upcoming.
  - morning briefing (Tier 18): once/day, reuses skills/daily_briefing.py.
  - idle/break reminder (Tier 18): nudges after too long an unbroken
    stretch of activity, built on automation/audit.py's own timestamps.
  - streak milestones (Tier 18): congratulates crossing 7/14/30/50/100 days
    on a habit (automation/streak_manager.py).
  - habit detection (Tier 18): "the same action at the same hour on 3+
    days" -> offers to save it as a workflow.
  - low disk (Tier 18): same armed/cooldown/hysteresis shape as battery,
    per mounted drive.

Every nudge's wording optionally gets a central LLM reword (_reword()
below) with the trigger's own plain template as the fallback - except
reminder follow-ups, whose wording already comes from reminders/
composer.py's own richer prompting (see ProactiveNudge.already_worded).

State for most of the above (cooldowns, which events/battery-dips/habits
have already been nudged, the one-at-a-time download watch) is
intentionally RAM-only, like brain/skill_loader.py's file-signature cache -
a restart re-arming a still-low battery nudge once, or losing an
in-progress download watch or activity-streak timer, is a harmless edge
case, and it keeps this engine mostly self-contained. Scheduled workflows
are the one deliberate exception (see above).
"""
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from collections import deque
from datetime import datetime, timedelta

from automation.audit import audit
from automation.workflow_scheduler import WorkflowScheduler
from brain import llm_provider
from database.db_manager import db
from monitoring.sys_monitor import SystemMonitor
from reminders import context as reminder_ctx
from reminders.composer import composer as reminder_composer
from settings.config import config

logger = logging.getLogger("ProactiveEngine")


@dataclass
class ProactiveNudge:
    kind: str      # see _PROACTIVE_KINDS below - also used as part of the audit tool name
    title: str
    message: str
    # Tier 18: True means `message` is ALREADY final wording (e.g.
    # reminder-followups, composed by reminders/composer.py's own richer,
    # stage-aware prompting) - _emit()'s centralized reword pass (_reword()
    # below) must leave it alone rather than asking an LLM to reword an
    # already-natural sentence a second time.
    already_worded: bool = False


# Every `kind` ProactiveNudge.kind actually takes, purely for
# documentation/grep-ability (nothing enforces this at runtime) - Tier 18
# grew this from 3 to 10.
_PROACTIVE_KINDS = ("battery", "calendar", "download", "workflow", "reminder_followup",
                    "morning_briefing", "idle_break", "streak", "habit", "low_disk")


# Tier 18: lazily-built-once-per-process LLM client for _reword() below -
# same lock/cache/resolve-once shape as reminders/composer.py's own
# _get_client_and_cfg() (deliberately duplicated, not imported: that one is
# reminders.composer's private module state, and this engine resolving its
# own provider independently means a provider misconfigured for one reword
# path can't silently take out the other).
_nudge_client_lock = threading.Lock()
_nudge_cached_client = None
_nudge_cached_cfg = None
_nudge_client_resolved = False


def _get_nudge_client_and_cfg():
    global _nudge_cached_client, _nudge_cached_cfg, _nudge_client_resolved
    with _nudge_client_lock:
        if not _nudge_client_resolved:
            _nudge_client_resolved = True
            try:
                cfg = llm_provider.resolve_provider()
                _nudge_cached_cfg = cfg
                _nudge_cached_client = llm_provider.build_client(cfg) if cfg.configured else None
            except Exception as e:
                logger.info(f"proactive: no LLM provider available for nudge rewording ({type(e).__name__}: {e})")
                _nudge_cached_client, _nudge_cached_cfg = None, None
        return _nudge_cached_client, _nudge_cached_cfg


# Tier 15: the handful of "this file isn't finished yet" suffixes browsers
# (and a few other download tools) use for an in-progress download. Not
# exhaustive of every tool that ever existed, but covers the common ones
# (Chrome/Edge's .crdownload, Firefox's .part, plus .download/.tmp as
# generic fallbacks) - good enough for "best-effort heads up", not a
# forensic guarantee.
_DOWNLOAD_TEMP_SUFFIXES = (".crdownload", ".part", ".download", ".tmp")


class ProactiveEngine:
    """Pure check logic lives in check_once() - synchronous, no sleep,
    independently testable without waiting on a real thread. start() wraps
    that in a loop on its own daemon thread; same split as
    brain/skill_loader.py's UserSkillLoader (scan_once() vs start())."""

    def __init__(self, interval: float = None):
        self.interval = interval if interval is not None else config.PROACTIVE_CHECK_INTERVAL
        self._listeners = []
        self._listeners_lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()

        self._battery_last_nudge_at = None   # None = never nudged yet (NOT 0.0 - time.monotonic()'s own
                                              # baseline is unspecified/arbitrary, so "elapsed since 0.0"
                                              # is meaningless and could even be falsely "recent" on some
                                              # systems; only compare elapsed time once there's a real
                                              # previous timestamp to compare against)
        self._battery_armed = True          # False while a low-battery nudge is "live"; re-arms once resolved
        self._nudged_event_ids = set()       # calendar event ids already given their heads-up nudge
        self._download_watch = None          # None = not watching; see arm_download_watch() for the shape
        self._workflow_runner = None         # Tier 18: see set_workflow_runner() below

        # Tier 18: the rest of the new triggers' own RAM state - same
        # "cheap, restart loses at most one in-progress timer" reasoning as
        # the battery/calendar/download state above.
        self._morning_briefing_last_date = None   # last calendar date the briefing fired on
        self._active_session_start = None         # start of the current unbroken activity run (or None)
        self._idle_break_nudged = False           # already nudged for THIS active_session_start
        self._streak_last_seen = {}                # habit -> highest streak length already milestone-nudged
        self._habit_tick_count = 0                 # _check_habit_detection runs on its own slower cadence
        self._habit_suggested = set()               # (tool, hour) pairs already suggested as a routine
        self._low_disk_armed = {}                    # device -> bool, same hysteresis shape as _battery_armed
        self._low_disk_last_nudge_at = {}             # device -> time.monotonic() of its last nudge
        self._recent_reworded = deque(maxlen=5)        # last few LLM-reworded lines - see _reword() below

    # ---- listeners ----
    def add_listener(self, callback):
        with self._listeners_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback):
        with self._listeners_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def set_workflow_runner(self, callback):
        """Tier 18: plugs in how a due scheduled workflow actually RUNS.
        This engine is deliberately Qt-free and holds no WaveBrain
        reference (see this module's own top docstring and
        ui/workflows_panel.py's _run_workflow, which needs
        self._main.brain._dispatch_tool - a GUI-side object this module
        never touches). `callback` must be a plain `fn(workflow_name: str)`
        that's safe to call from a NON-GUI thread (this engine's own
        daemon thread) - ui/main_window.py wires one in that hands off to
        self.worker_pool.submit(...), the same Qt-thread-pool-backed,
        any-thread-safe call ui/workflows_panel.py's own "Run Now" button
        already uses, so a scheduled run and a manual run share the exact
        same execution path and audit/undo treatment. None (the default)
        means due schedules are still detected and advanced/disabled
        on time (so nothing double-fires once a runner IS wired in), they
        just don't get run yet - e.g. before the GUI has finished starting up."""
        self._workflow_runner = callback

    def _reword(self, nudge: ProactiveNudge) -> str:
        """Tier 18: one central LLM-reword-with-template-fallback, applied
        here rather than scattered per-trigger - every _check_* method above
        still only ever produces its own plain, deterministic sentence
        (unchanged); this is what optionally makes it sound a little more
        natural before it reaches the user. Synchronous (unlike
        reminders/composer.py's start()/wait() split) because check_once()
        has no beep-timing to protect - there's nothing worth overlapping
        the LLM call with here. Falls back to the template VERBATIM (no
        delay worth mentioning - resolve_provider()/build_client() happen
        at most once per process) whenever reword is disabled, no provider
        is configured, the call fails/times out, or the reply doesn't pass
        reminders/context.py's own validate_text() - reused as-is since its
        checks (empty/markdown/too long/a hallucinated number/an exact
        repeat) are just as relevant to a nudge as to a reminder, and
        duplicating that logic here would be the real risk."""
        if not config.PROACTIVE_LLM_REWORD_ENABLED:
            return nudge.message
        client, cfg = _get_nudge_client_and_cfg()
        if client is None or cfg is None:
            return nudge.message
        try:
            response = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": (
                        "Reword the following short notification so it sounds natural, warm, and "
                        "conversational, as if a helpful voice assistant were saying it out loud. Keep it "
                        "to one short sentence, keep every number and name in it exactly as given, and "
                        "don't add any new information. Reply with ONLY the reworded sentence, nothing else.")},
                    {"role": "user", "content": nudge.message},
                ],
                model=cfg.model,
                temperature=0.6,
                max_tokens=100,
                # Deliberately short, same reasoning as composer.py's own
                # CONTEXTUAL_REMINDER_LLM_TIMEOUT_S comment: fail fast, the
                # template fallback is right there and just as correct.
                timeout=config.PROACTIVE_LLM_REWORD_TIMEOUT_S,
            )
            raw = ((response.choices[0].message.content if response and response.choices else "") or "").strip()
        except Exception as e:
            logger.info(f"proactive: nudge reword failed, using the template ({type(e).__name__}: {e})")
            return nudge.message

        allowed_numbers = [int(n) for n in re.findall(r"\d+", nudge.message)]
        cleaned = reminder_ctx.validate_text(raw, config.PROACTIVE_LLM_REWORD_MAX_WORDS,
                                             allowed_numbers=allowed_numbers, recent=list(self._recent_reworded))
        if not cleaned:
            return nudge.message
        self._recent_reworded.append(cleaned)
        return cleaned

    def _emit(self, nudge: ProactiveNudge):
        if not nudge.already_worded:
            nudge.message = self._reword(nudge)
        with self._listeners_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(nudge)
            except Exception:
                logger.exception("Proactive listener raised")
        try:
            audit.record(f"proactive_{nudge.kind}", {"title": nudge.title}, result=nudge.message,
                        status="ok", source="proactive")
        except Exception:
            logger.exception("Couldn't audit-log a proactive nudge")
        # Tier 20 (#88): "why did you notify me?" needs a record of WHY a
        # given nudge fired, not just THAT it fired (audit.py's job above).
        # The nudge's own title+message already states the trigger in plain
        # language (e.g. "Battery is at 12% and not charging") since that's
        # what the person actually heard, so reusing it as the security
        # log's `reason` costs nothing extra and can't drift out of sync
        # with what was really said. Lands in the SAME log as guard
        # decisions (automation/security_log.py) rather than a separate
        # proactive-only log - see that module's own "why a second log"
        # docstring section for the reasoning that applies here too: one
        # ring buffer, one "explain the last thing that happened" tool,
        # distinguished by `kind`.
        try:
            from automation import security_log
            security_log.record("proactive_notify", reason=f"{nudge.title}: {nudge.message}",
                                 source="proactive", tier=nudge.kind)
        except Exception:
            logger.exception("Couldn't security-log a proactive nudge")

    # ---- checks ----
    def _check_battery(self) -> list:
        if not config.PROACTIVE_BATTERY_ENABLED:
            return []
        stats = SystemMonitor.get_stats()
        percent = stats.get("battery_percent")
        if not isinstance(percent, (int, float)):
            return []  # "N/A" on a desktop with no battery sensor - nothing to watch
        is_plugged = bool(stats.get("is_plugged"))

        if is_plugged or percent > config.PROACTIVE_BATTERY_THRESHOLD:
            if not self._battery_armed:
                # Resolved after actually being low (not just staying high the
                # whole time) - the cooldown is scoped to repeats WITHIN one
                # still-low episode (see config.py's own comment on it), so a
                # genuinely new episode next time it drops shouldn't inherit
                # this one's cooldown timer.
                self._battery_last_nudge_at = None
            self._battery_armed = True
            return []
        if not self._battery_armed:
            return []  # still low, already nudged, not yet re-armed

        now = time.monotonic()
        if self._battery_last_nudge_at is not None and \
           now - self._battery_last_nudge_at < config.PROACTIVE_BATTERY_COOLDOWN_MIN * 60:
            return []

        self._battery_last_nudge_at = now
        self._battery_armed = False
        return [ProactiveNudge(
            kind="battery", title="Battery low",
            message=f"Heads up - your battery's at {percent:.0f}% and not charging. Might want to plug in.",
        )]

    def _check_calendar(self) -> list:
        if not config.PROACTIVE_CALENDAR_ENABLED:
            return []
        try:
            rows = db.get_upcoming_calendar_events(config.PROACTIVE_CALENDAR_LEAD_MINUTES)
        except Exception:
            logger.exception("Couldn't check upcoming calendar events")
            return []

        nudges = []
        for row in rows:
            event_id, title, start_at = row[0], row[1], row[2]
            # Tier 8: keyed on (id, start_at) rather than bare id - once
            # get_calendar_events_between() started expanding a recurring
            # event into one virtual row per occurrence (see that
            # method's docstring in database/db_manager.py), a bare id
            # key would've suppressed every occurrence after the first
            # FOREVER, since a recurring event keeps the same real row id
            # for every occurrence. Keying on the pair re-arms the nudge
            # for each new occurrence while still nudging a plain one-off
            # event exactly once, same as before.
            dedup_key = (event_id, start_at)
            if dedup_key in self._nudged_event_ids:
                continue
            self._nudged_event_ids.add(dedup_key)
            nudges.append(ProactiveNudge(
                kind="calendar", title="Upcoming event",
                message=f"Heads up - '{title}' starts in about {config.PROACTIVE_CALENDAR_LEAD_MINUTES} minutes.",
            ))
        # Keep the seen-ids set from growing forever over a long-running session.
        if len(self._nudged_event_ids) > 500:
            self._nudged_event_ids = set(list(self._nudged_event_ids)[-200:])
        return nudges

    # ---- download watch (Tier 15) ----
    def arm_download_watch(self, folder: str = None, timeout_minutes: float = None) -> str:
        """Snapshots `folder` right now and arms the watch; _check_downloads
        compares every later tick against this snapshot. Replaces any watch
        already running (one at a time - arming a second just re-points the
        same single watch, rather than two running in parallel and either
        one's completion going unexplained about which download it meant)."""
        target = folder or os.path.join(os.path.expanduser("~"), "Downloads")
        minutes = timeout_minutes if timeout_minutes is not None else config.DOWNLOAD_WATCH_TIMEOUT_MIN
        try:
            baseline = set(os.listdir(target))
        except OSError as e:
            return f"Couldn't watch {target}: {e}"
        self._download_watch = {
            "folder": target,
            "baseline": baseline,
            "seen_temp": {e for e in baseline if e.lower().endswith(_DOWNLOAD_TEMP_SUFFIXES)},
            "timeout_at": time.monotonic() + minutes * 60,
            "timeout_minutes": minutes,
        }
        return (f"Watching {target} for a new download - I'll let you know when one finishes "
                f"(or after about {minutes:.0f} minutes if I don't see one).")

    def cancel_download_watch(self) -> str:
        if self._download_watch is None:
            return "I wasn't watching for a download."
        folder = self._download_watch["folder"]
        self._download_watch = None
        return f"Stopped watching {folder} for a download."

    def _check_downloads(self) -> list:
        if not config.PROACTIVE_DOWNLOAD_WATCH_ENABLED:
            return []
        watch = self._download_watch
        if watch is None:
            return []   # by far the common case - nothing armed, nothing to do

        if time.monotonic() >= watch["timeout_at"]:
            self._download_watch = None
            return [ProactiveNudge(
                kind="download", title="Download watch timed out",
                message=f"I didn't see a new download finish in {watch['folder']} within "
                        f"about {watch['timeout_minutes']:.0f} minutes, so I've stopped watching.",
            )]

        try:
            current = set(os.listdir(watch["folder"]))
        except OSError:
            logger.exception(f"Couldn't list {watch['folder']} for the download watch")
            self._download_watch = None
            return []

        # Completion signal #1: a brand new file that ISN'T itself a
        # still-downloading temp file - the common case for a file that
        # didn't already exist under its final name before this watch armed.
        completed_name = None
        for entry in current - watch["baseline"]:
            if not entry.lower().endswith(_DOWNLOAD_TEMP_SUFFIXES):
                completed_name = entry
                break

        # Completion signal #2: a temp file that WAS there (at arm time or
        # as of the last tick) has since disappeared, and its final-named
        # counterpart now exists - catches a download finishing under a
        # filename that already existed before the watch started (so
        # signal #1 above would never have seen it as "new").
        if completed_name is None:
            for temp_name in watch["seen_temp"] - current:
                for suffix in _DOWNLOAD_TEMP_SUFFIXES:
                    if temp_name.lower().endswith(suffix) and temp_name[:-len(suffix)] in current:
                        completed_name = temp_name[:-len(suffix)]
                        break
                if completed_name:
                    break

        watch["seen_temp"] |= {e for e in current if e.lower().endswith(_DOWNLOAD_TEMP_SUFFIXES)}

        if completed_name is None:
            return []
        self._download_watch = None
        return [ProactiveNudge(
            kind="download", title="Download finished",
            message=f"Looks like '{completed_name}' just finished downloading to {watch['folder']}.",
        )]

    # ---- scheduled workflows (Tier 18) ----
    def _check_scheduled_workflows(self) -> list:
        if not config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED:
            return []
        try:
            due_names = WorkflowScheduler.get_due_schedules()
        except Exception:
            logger.exception("Couldn't check scheduled workflows")
            return []

        nudges = []
        for wf_name in due_names:
            if self._workflow_runner is not None:
                try:
                    self._workflow_runner(wf_name)
                except Exception:
                    # A GUI-side problem running the workflow must not take
                    # down this engine's own tick - the schedule's already
                    # been advanced/disabled by get_due_schedules() above,
                    # same as any other due-but-failed trigger.
                    logger.exception(f"Workflow runner raised for scheduled workflow '{wf_name}'")
            else:
                logger.warning(f"Scheduled workflow '{wf_name}' is due but no runner is wired in yet - skipping.")
            # The run's own result (success or failure) shows up separately,
            # the same way ui/workflows_panel.py's "Run Now" button posts its
            # own chat bubble via _dispatch_tool's audited result - this
            # nudge is just the ambient "something is about to happen"
            # heads-up, not the run's outcome.
            nudges.append(ProactiveNudge(
                kind="workflow", title="Scheduled workflow",
                message=f"Running your scheduled '{wf_name}' workflow now.",
            ))
        return nudges

    # ---- reminder follow-ups (Tier 18, migrated from ReminderWorker) ----
    def _check_reminder_followups(self) -> list:
        """Moved here from ui/workers.py's ReminderWorker - see that
        class's own docstring. due_at/upcoming stay in ReminderWorker (the
        user explicitly asked to be told something at a time THEY set);
        this one is Wave noticing unprompted that some time has passed
        since a reminder fired with no acknowledgment - the genuinely
        ambient half of what that worker used to do. Logic/wording are
        otherwise unchanged: same db.get_due_followups()/mark_followup_sent(),
        same reminders/composer.py composition. already_worded=True on the
        nudge, since composer.py's own stage-aware prompting (which knows
        about `minutes_since`, reminder categories, etc.) already does a
        better job than this engine's generic _reword() ever could -
        rewording its output a second time would be pure downside."""
        if not config.PROACTIVE_REMINDER_FOLLOWUPS_ENABLED:
            return []
        try:
            followups = db.get_due_followups()  # (id, message, delivered_at)
        except Exception:
            logger.exception("Couldn't check reminder follow-ups")
            return []
        if not followups:
            return []

        # Starts every composition before waiting on any of them - same
        # "N due in one tick cost one LLM round-trip, not N" reasoning as
        # ReminderWorker._compose_all(), which this inlines (that method is
        # a QThread-class staticmethod - not worth importing ui/workers.py
        # into this Qt-free module just to reuse four lines of it).
        pending = [
            (reminder_id, message, delivered_at,
             reminder_composer.start(message, stage="followup", minutes_since=reminder_ctx.minutes_since(delivered_at)))
            for reminder_id, message, delivered_at in followups
        ]
        nudges = []
        for reminder_id, _message, _delivered_at, waiter in pending:
            try:
                db.mark_followup_sent(reminder_id)
            except Exception:
                logger.exception(f"Couldn't mark reminder {reminder_id}'s follow-up as sent")
                continue
            result = waiter()
            nudges.append(ProactiveNudge(
                kind="reminder_followup", title="Reminder follow-up",
                message=result.text, already_worded=True,
            ))
        return nudges

    # ---- morning briefing (Tier 18) ----
    def _check_morning_briefing(self) -> list:
        """Once per calendar day, the first tick at/after
        config.PROACTIVE_MORNING_BRIEFING_TIME - reuses
        skills/daily_briefing.py's run_daily_briefing() directly (the exact
        same weather+calendar+reminders summary 'give me my daily briefing'
        already produces) rather than reimplementing it."""
        if not config.PROACTIVE_MORNING_BRIEFING_ENABLED:
            return []
        now = datetime.now()
        if self._morning_briefing_last_date == now.date():
            return []
        try:
            target = datetime.strptime(config.PROACTIVE_MORNING_BRIEFING_TIME, "%H:%M").time()
        except ValueError:
            logger.error(f"PROACTIVE_MORNING_BRIEFING_TIME '{config.PROACTIVE_MORNING_BRIEFING_TIME}' isn't "
                        f"a valid HH:MM - morning briefing disabled until it's fixed.")
            return []
        if now.time() < target:
            return []

        self._morning_briefing_last_date = now.date()
        try:
            # Local import: skills/daily_briefing.py -> web/web_ops.py has a
            # hard top-level `import pywhatkit` - a heavy, WhatsApp-specific
            # dependency this otherwise-lean module has no other reason to
            # drag in at IMPORT time for every caller of automation.proactive
            # (including tests that never touch morning briefings). Paid
            # only once this check actually runs, same deferred-cost idea as
            # check_once()'s own local AppController import just above,
            # different reason (avoiding a heavy transitive import, not
            # breaking a cycle).
            from skills.daily_briefing import run_daily_briefing
            text = run_daily_briefing()
        except Exception:
            logger.exception("Couldn't build the morning briefing")
            return []
        return [ProactiveNudge(kind="morning_briefing", title="Morning briefing", message=text)]

    # ---- idle/break reminder (Tier 18) ----
    def _check_idle_break(self) -> list:
        """"Idle" is the RESET condition here, not the nudge itself -
        nudging someone who's stepped away is pointless. The actual nudge
        is a break reminder after too much UNBROKEN activity: reuses
        automation/audit.py's own timestamps (zero new tracking needed) to
        find how far back the user's current run of actions goes without a
        gap longer than PROACTIVE_IDLE_RESET_GAP_MIN, the same "build on
        audit.recent(), no new DB method" precedent
        brain/followup_resolver.py already set in Tier 17."""
        if not config.PROACTIVE_IDLE_BREAK_ENABLED:
            return []
        try:
            records = audit.recent(limit=200)
        except Exception:
            logger.exception("Couldn't read recent activity for the idle/break check")
            return []
        if not records:
            self._active_session_start = None
            self._idle_break_nudged = False
            return []

        now = datetime.now()
        gap = timedelta(minutes=config.PROACTIVE_IDLE_RESET_GAP_MIN)
        try:
            latest_ts = datetime.fromisoformat(records[0].ts)
        except (TypeError, ValueError):
            return []
        if now - latest_ts > gap:
            self._active_session_start = None  # gone quiet - no active run right now
            self._idle_break_nudged = False
            return []

        # records is newest-first; walk backward in time until a gap breaks
        # the unbroken run (or the fetched window runs out, which just
        # means "at least this long" - good enough for a break reminder).
        session_start = latest_ts
        prev_ts = latest_ts
        for rec in records[1:]:
            try:
                ts = datetime.fromisoformat(rec.ts)
            except (TypeError, ValueError):
                break
            if prev_ts - ts > gap:
                break
            session_start, prev_ts = ts, ts

        if self._active_session_start != session_start:
            self._active_session_start = session_start  # a fresh run - re-arm
            self._idle_break_nudged = False
        if self._idle_break_nudged:
            return []
        if now - session_start < timedelta(minutes=config.PROACTIVE_BREAK_AFTER_MIN):
            return []

        self._idle_break_nudged = True
        minutes = int((now - session_start).total_seconds() // 60)
        return [ProactiveNudge(
            kind="idle_break", title="Time for a break?",
            message=f"You've been at this for about {minutes} minutes straight - might be worth a quick break.",
        )]

    # ---- streak milestones (Tier 18) ----
    _STREAK_MILESTONES = (7, 14, 30, 50, 100)

    def _check_streak_milestones(self) -> list:
        """RAM-tracked highest streak length already congratulated, per
        habit (automation/streak_manager.py's own db.list_streaks()) -
        nudges once on crossing each milestone in _STREAK_MILESTONES. A
        habit already past a milestone the FIRST time this ever runs (e.g.
        a pre-existing 45-day streak right after this feature ships) nudges
        once for the highest milestone already reached, not once per
        milestone - a "welcome back" acknowledgment, not a backlog of
        congratulations. A reset-to-1 (StreakManager.check_in's own missed-
        day behavior) re-arms every milestone for next time the streak
        climbs back through it; StreakManager.reset() deleting the row
        entirely drops it from this dict too, so starting over truly starts
        over."""
        if not config.PROACTIVE_STREAK_MILESTONES_ENABLED:
            return []
        try:
            rows = db.list_streaks()  # (id, habit, current_streak, best_streak, last_checkin_date)
        except Exception:
            logger.exception("Couldn't read streaks for the milestone check")
            return []

        nudges = []
        seen_habits = set()
        for _id, habit, current, _best, _last in rows:
            seen_habits.add(habit)
            last_seen = self._streak_last_seen.get(habit, 0)
            crossed = [m for m in self._STREAK_MILESTONES if last_seen < m <= current]
            if crossed:
                nudges.append(ProactiveNudge(
                    kind="streak", title="Streak milestone",
                    message=f"Nice - you just hit a {max(crossed)}-day streak on '{habit}'!",
                ))
            self._streak_last_seen[habit] = current
        for habit in list(self._streak_last_seen):
            if habit not in seen_habits:
                del self._streak_last_seen[habit]
        return nudges

    # ---- habit detection (Tier 18) ----
    # A short, defensible list of meta/read-only tools - acting on these
    # reveals nothing about a habit worth turning into a workflow (a
    # workflow that just "lists your reminders" every morning isn't the
    # kind of routine this is meant to catch).
    _HABIT_EXCLUDED_TOOLS = frozenset({
        "list_reminders", "list_calendar_events", "list_workflows", "list_workflow_schedules",
        "list_streaks", "get_streak_status", "list_recent_actions", "get_active_mode",
        "run_self_diagnostics", "daily_briefing", "read_screen_text", "describe_screen",
        "get_disk_usage", "get_battery_status", "get_uptime", "get_local_ip",
        "get_cpu_temperature", "get_gpu_status", "get_top_processes", "recall_memory",
    })
    _HABIT_MIN_DISTINCT_DAYS = 3
    _HABIT_CHECK_EVERY_N_TICKS = 20  # its own slower cadence - no need to re-scan 500 audit rows every tick

    def _check_habit_detection(self) -> list:
        """"The same action at the same hour on 3+ distinct days" - groups
        automation/audit.py's own audit.recent() by (tool, hour-of-day)
        across distinct calendar dates, the same "build on audit.recent(),
        no new DB method" precedent _check_idle_break and Tier 17's
        followup_resolver.py both already use. Suggests saving it as a
        workflow once per (tool, hour) pair (RAM-tracked in
        self._habit_suggested) - doesn't actually create the workflow
        itself, just asks; create_workflow is a confirm-worthy T1 tool, not
        something to call unprompted."""
        if not config.PROACTIVE_HABIT_DETECTION_ENABLED:
            return []
        self._habit_tick_count += 1
        if self._habit_tick_count % self._HABIT_CHECK_EVERY_N_TICKS != 0:
            return []
        try:
            records = audit.recent(limit=500)
        except Exception:
            logger.exception("Couldn't read recent activity for habit detection")
            return []

        groups = {}
        for rec in records:
            if rec.tool in self._HABIT_EXCLUDED_TOOLS or rec.status != "ok":
                continue
            try:
                ts = datetime.fromisoformat(rec.ts)
            except (TypeError, ValueError):
                continue
            groups.setdefault((rec.tool, ts.hour), set()).add(ts.date())

        nudges = []
        for (tool, hour), dates in groups.items():
            if len(dates) < self._HABIT_MIN_DISTINCT_DAYS or (tool, hour) in self._habit_suggested:
                continue
            self._habit_suggested.add((tool, hour))
            phrase = tool.replace("_", " ")
            nudges.append(ProactiveNudge(
                kind="habit", title="Routine detected",
                message=f"I've noticed you often {phrase} around {hour}:00 - want me to save that as a workflow?",
            ))
        return nudges

    # ---- low disk space (Tier 18) ----
    def _check_low_disk(self) -> list:
        """Same armed/cooldown/hysteresis shape as _check_battery above,
        just per-device (a dict keyed by device, not one bool/timestamp) -
        monitoring.sys_monitor.py's new get_disk_partitions_usage() (pulled
        out of get_disk_usage() this same tier) gives the real numbers to
        threshold on."""
        if not config.PROACTIVE_LOW_DISK_ENABLED:
            return []
        try:
            partitions = SystemMonitor.get_disk_partitions_usage()
        except Exception:
            logger.exception("Couldn't check disk usage")
            return []

        nudges = []
        seen_devices = set()
        for p in partitions:
            device = p["device"]
            seen_devices.add(device)
            if p["percent"] < config.PROACTIVE_LOW_DISK_THRESHOLD:
                self._low_disk_armed[device] = True
                continue
            if not self._low_disk_armed.get(device, True):
                continue

            now = time.monotonic()
            last = self._low_disk_last_nudge_at.get(device)
            if last is not None and now - last < config.PROACTIVE_LOW_DISK_COOLDOWN_MIN * 60:
                continue

            self._low_disk_last_nudge_at[device] = now
            self._low_disk_armed[device] = False
            nudges.append(ProactiveNudge(
                kind="low_disk", title="Low disk space",
                message=f"Heads up - {device} is at {p['percent']:.0f}% full, only "
                        f"{p['free_gb']:.1f} GB free.",
            ))
        for device in list(self._low_disk_armed):
            if device not in seen_devices:
                del self._low_disk_armed[device]
                self._low_disk_last_nudge_at.pop(device, None)
        return nudges

    def check_once(self) -> list:
        """Runs every trigger once and returns whatever nudges fired - the
        pure, synchronous core that start()'s loop (and tests) call into.
        A trigger raising is logged and treated as "nothing to report"
        rather than taking down the whole check.

        Tier 18: DND/focus/sleep gating happens HERE, before any trigger
        runs at all - not inside _emit() after a trigger's already fired,
        and not per-trigger inside each check method. Gating after a check
        has already run and mutated its own dedup/cooldown state (e.g.
        _check_battery flipping self._battery_armed, or
        _check_scheduled_workflows advancing a schedule past its due time)
        would silently consume that nudge for good, even once DND ends -
        gating BEFORE any check runs means nothing is consumed this tick,
        so whatever would have fired naturally fires on the very next
        post-DND tick instead. Uses AppController.is_dnd_or_focus_active()
        (a plain, unaudited dict read), never the audited get_active_mode()
        tool - that would write one audit-log row per engine tick forever.
        Local import: see automation/app_control.py's own top-level
        `from automation.proactive import proactive_engine` - importing it
        back from here at module level would be a circular import."""
        from automation.app_control import AppController
        try:
            if AppController.is_dnd_or_focus_active():
                return []
        except Exception:
            logger.exception("Couldn't check DND/focus state - proceeding as if it's off")

        nudges = []
        for check in (self._check_battery, self._check_calendar, self._check_downloads,
                      self._check_scheduled_workflows, self._check_reminder_followups,
                      self._check_morning_briefing, self._check_idle_break,
                      self._check_streak_milestones, self._check_habit_detection,
                      self._check_low_disk):
            try:
                nudges.extend(check())
            except Exception:
                logger.exception(f"Proactive check {check.__name__} raised")
        for nudge in nudges:
            self._emit(nudge)
        return nudges

    # ---- lifecycle ----
    def _run(self):
        logger.info(f"Proactive engine started (every {self.interval}s).")
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("Proactive engine tick failed")
            self._stop_event.wait(self.interval)

    def start(self):
        if not config.PROACTIVE_ENABLED:
            logger.info("Proactive engine disabled (PROACTIVE_ENABLED=false).")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="WaveProactive", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def is_alive(self) -> bool:
        """Tier 19: lets automation/thread_supervisor.py watch this thread
        and restart it if it ever dies unexpectedly (see that module's
        own docstring for why a dead background thread can otherwise go
        unnoticed)."""
        return self._thread is not None and self._thread.is_alive()


proactive_engine = ProactiveEngine()

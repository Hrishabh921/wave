"""
brain/handlers/avatar.py - Tier 25: show_ui/hide_ui, so "wave ka ui dikhao" /
"show the UI" and "ui chhupao" / "hide the UI" dispatch exactly like every
other @skill(...) tool (see brain/handlers/__init__.py's own module
docstring for why this package is split one file per domain - this is
domain #12, added once Wave had an avatar companion worth hiding the
dashboard behind, see docs/ROADMAP_BEAT_SARA.md's Tier 25 section).

Neither handler imports PyQt6, or avatar/control.py's AvatarController
class, at all - only avatar/registry.py's get_active_controller(), which
has zero imports of its own (see that module's docstring for why it's a
separate file from avatar/control.py specifically: control.py wraps
QObject/QMetaObject, so importing IT here would make PyQt6 a transitive
hard dependency of brain/, breaking every brain-importing test in any
environment without PyQt6 installed). This file, and the rest of brain/,
stays Qt-free and importable with no display at all, same as every other
handlers/ file. The real AvatarController instance is created once by
ui/main_window.py's MainWindow._init_avatar() (on the GUI thread) and
registered via avatar.registry.set_active_controller() right after - see
that method's own comment for where.

If no controller has been registered yet - no avatar rig has been built
this run (see avatar/rig.py), or this is the web UI (webui/bridge.py/
wave_web_start.pyw, which this round's avatar work deliberately does NOT
cover - see Tier 25's "Status" note for why: pywebview's native window
host doesn't share a Qt event loop with a floating PyQt6 widget) - both
skills say so plainly rather than raising, so a stray "show the UI" gets
an honest answer instead of a crash.
"""
from brain.skills_registry import skill
from avatar.registry import get_active_controller

_NO_CONTROLLER_MESSAGE = (
    "Avatar/dashboard toggle abhi yahan available nahi hai - ya to avatar "
    "set up nahi hua is session mein, ya ye web UI hai jo filhal is feature "
    "mein shaamil nahi hai."
)


@skill("show_ui", "Shows Wave's full dashboard window. Use this when the user asks to show, "
       "open, or turn on Wave's UI/dashboard/window/interface (e.g. 'wave ka ui dikhao', "
       "'show the UI', 'open the dashboard'). The avatar keeps running either way - this "
       "only affects the separate full-dashboard window.")
def _skill_show_ui(brain, args: dict) -> str:
    controller = get_active_controller()
    if controller is None:
        return _NO_CONTROLLER_MESSAGE
    return controller.show_ui()


@skill("hide_ui", "Hides Wave's full dashboard window, leaving only the avatar visible. Use "
       "this when the user asks to hide, close, or turn off Wave's UI/dashboard/window/"
       "interface (e.g. 'ui chhupao', 'hide the UI', 'close the dashboard') - this is NOT "
       "the same as closing or quitting Wave itself.")
def _skill_hide_ui(brain, args: dict) -> str:
    controller = get_active_controller()
    if controller is None:
        return _NO_CONTROLLER_MESSAGE
    return controller.hide_ui()

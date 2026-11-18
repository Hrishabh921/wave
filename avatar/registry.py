"""
avatar/registry.py — Tier 25: the Qt-free half of the show_ui/hide_ui seam.

Holds nothing but a module-level singleton and two accessor functions -
split out of avatar/control.py specifically so brain/handlers/avatar.py
can depend on THIS module instead of avatar/control.py directly and stay
exactly as Qt-free as every other file in brain/ (see brain/handlers/
__init__.py's own module docstring for why that matters: importing
brain.handlers must work with no display and no PyQt6 installed at all,
and avatar/control.py - which wraps QObject/QMetaObject for its
cross-thread marshalling - cannot be imported without PyQt6). Before this
split existed, brain/handlers/avatar.py imported avatar.control directly,
which made PyQt6 a transitive hard dependency of brain/ the moment that
file was added - breaking every brain-importing test in any environment
without PyQt6 installed (this sandbox included). This file is the fix:
it's the ONLY thing brain/handlers/avatar.py touches, and it has zero
imports of its own.

avatar/control.py re-exports both names (see its own top) purely for
convenience - ui/main_window.py, which is already Qt-heavy, has no reason
to know or care that the split exists; only brain/ does.
"""
_active_controller = None


def set_active_controller(controller):
    """Called once by ui/main_window.py's MainWindow._init_avatar() right
    after constructing the real AvatarController - also used by tests to
    install/clear a fake one."""
    global _active_controller
    _active_controller = controller


def get_active_controller():
    """None when no avatar has been set up this run (no rig built yet,
    or this is the web UI - see docs/ROADMAP_BEAT_SARA.md's Tier 25
    section for why the web UI is out of scope this round) - callers
    (brain/handlers/avatar.py) treat that as "nothing to show/hide"
    rather than an error."""
    return _active_controller

"""Example drop-in skill for Wave (this file is ignored because it starts with "_").

HOW TO USE
  1. Copy it to a name that does NOT start with "_"   ->  user_skills/dice.py
  2. Save. Within ~2 seconds Wave logs "Skill loaded" and shows a toast.
     No restart needed. Say: "roll a dice" / "teen baar dice phenko".
  3. Edit the file and save again - the new version replaces the old one.
     If you make a typo, the previous working version keeps running and the
     error is shown instead. Delete the file to remove the skill.

RULES OF THE ROAD
  * `@skill(name, description, params, required)` - the description is what the
    AI reads to decide when to call your function, so write it like an
    instruction. `params` is a JSON-schema-ish dict; `required` lists names
    from `params`.
  * Your function receives `(brain, args)`: `brain` is the running WaveBrain,
    `args` is a dict of what the AI filled in. Return a short string - that
    string is what Wave reports back.
  * Names: letters, digits, "_" and "-" only (max 64). You can't reuse the
    name of a built-in skill or of a skill in another file.
  * Don't start threads or open long-lived resources at import time; a reload
    runs this file again.
  * This runs with the same permissions as Wave. Only drop in code you trust.
"""
import random

from brain.skills_registry import skill


@skill(
    "roll_dice",
    "Rolls a dice (or several) and reports what came up. Use when the user asks to roll a dice, "
    "flip a number, or pick a random number between 1 and N.",
    {
        "sides": {"type": "integer", "description": "How many sides the dice has. Defaults to 6."},
        "count": {"type": "integer", "description": "How many dice to roll. Defaults to 1."},
    },
    [],
)
def roll_dice(brain, args):
    sides = max(2, int(args.get("sides") or 6))
    count = min(20, max(1, int(args.get("count") or 1)))
    rolls = [random.randint(1, sides) for _ in range(count)]
    if count == 1:
        return f"You rolled a {rolls[0]} (d{sides})."
    return f"You rolled {', '.join(map(str, rolls))} - total {sum(rolls)} (d{sides} x{count})."

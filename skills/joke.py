"""
skills/joke.py — "tell me a joke" / "ek joke sunao".

A small, curated, fully offline list - no LLM call, no network, so this
always works even with no internet, costs nothing to run, and never has
a surprising off-day. Deliberately not LLM-generated on demand: a model
asked for "a joke" tends to reach for the same handful of jokes anyway,
and a fixed list this codebase can actually read and vouch for beats an
unreviewed one a model might produce.
"""
import random

_JOKES = [
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my computer I needed a break, and now it won't stop sending me vacation ads.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I asked the Wi-Fi for a password. It said 'you're already connected, stop asking.'",
    "Why did the developer go broke? Because they used up all their cache.",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
    "Main apni memory consolidation feature test kar raha tha, aur bhool gaya ki chutkula sunana tha. Fir yaad aaya.",
    "I told my assistant to remind me to tell a joke. It just told me this one.",
    "Why did the laptop go to the doctor? It had a virus and kept crashing the appointment.",
    "A SQL query walks into a bar, walks up to two tables, and asks: 'Can I join you?'",
    "Why don't programmers like nature? It has too many bugs and no documentation.",
    "Ek baar ek user ne Wave se poocha, 'tum sach mein smart ho?' Wave bola, 'main apna khud ka code 2000 lines se chhota kar chuka hoon, tum batao.'",
    "I would tell you a UDP joke, but you might not get it.",
    "Why did the timer and the reminder stop talking? They couldn't agree on whose turn it was.",
    "There are only 10 types of people: those who understand binary, and those who don't.",
]


def tell_joke() -> str:
    return random.choice(_JOKES)

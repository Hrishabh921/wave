"""
bench — Tier 4 item #12: an eval/regression harness for brain/intent_router.py
(router mode, against golden/*.jsonl) and brain/content_trust.py (redteam
mode, against redteam/*.jsonl), plus a live check that Wave's confirm-gated
tools are still actually gated. See bench/README.md and `python -m
bench.run_bench --help`.
"""

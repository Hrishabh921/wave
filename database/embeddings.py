"""
database/embeddings.py — Phase 3

Optional semantic-embedding helper for Wave's `memory` table.

Uses sentence-transformers (all-MiniLM-L6-v2 by default - 384 dimensions,
small and fast enough to run locally on CPU) to embed remembered facts, so
db_manager.get_relevant_memory() can surface facts that are
*conceptually* related to what the user just said, instead of only ones
that match by an exact key lookup.

Entirely optional, same philosophy as the Ollama fallback (Phase 2) and
the faster-whisper STT migration (also Phase 3): if the
'sentence-transformers' package isn't installed, or the model can't be
loaded for any reason (no internet on first run to fetch weights, etc.),
every function below degrades to None/[] and callers fall back to exactly
the plain get_all_memory() behavior that existed before this file did.

cosine_similarity() is pure Python - no numpy import here at all. numpy is
already a transitive dependency of sentence-transformers itself (needed to
actually produce an embedding), but keeping the *comparison* math
dependency-free means an already-embedded memory row can still be
compared against even in a hypothetical environment where numpy/torch
got removed later but old embeddings are still sitting in the database -
only generating a *new* embedding needs the model.
"""
import json
import logging
import math

from settings.config import config

logger = logging.getLogger("Embeddings")

# The canonical, fully-qualified Hub id (rather than the bare model name)
# so there's no ambiguity about which org/repo this resolves to.
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_model = None
_load_failed = False


def _get_model():
    """Lazily loads and caches the sentence-transformers model. Returns
    None - never raises - if the package isn't installed or the model
    can't be loaded (e.g. no internet on first run to fetch weights from
    the Hub), and remembers that failure so every subsequent call doesn't
    pay the cost of retrying a load that's already failed once this
    session."""
    global _model, _load_failed
    if not config.SEMANTIC_MEMORY_ENABLED:
        return None
    if _load_failed:
        return None
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        logger.info("Semantic memory unavailable: 'sentence-transformers' not installed "
                    "- falling back to plain key/value memory recall.")
        _load_failed = True
        return None
    try:
        _model = SentenceTransformer(MODEL_NAME)
        logger.info(f"Loaded semantic memory model '{MODEL_NAME}'.")
        return _model
    except Exception as e:
        logger.warning(f"Semantic memory model failed to load, falling back to plain "
                        f"key/value memory recall: {e}")
        _load_failed = True
        return None


def embed(text: str):
    """Returns `text`'s embedding as a plain list[float] (JSON/SQLite-
    friendly), or None if semantic embeddings aren't available in this
    environment, or `text` is empty. Never raises."""
    if not text or not text.strip():
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()
    except Exception as e:
        logger.warning(f"Embedding a memory entry failed, it will be stored "
                        f"without one: {e}")
        return None


def cosine_similarity(a, b) -> float:
    """Pure-Python cosine similarity between two equal-length vectors.
    Returns 0.0 (never raises) for mismatched/empty/zero-norm input,
    which naturally sorts to the bottom of any similarity-ranked list
    rather than crashing recall for one malformed row."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def serialize(vector):
    """Embedding (list[float]) -> the TEXT the `memory.embedding` column
    actually stores, or None (so sqlite3 binds real SQL NULL, not the
    3-character text "null") when there's no vector to store - callers
    don't need to special-case a None embedding themselves."""
    if vector is None:
        return None
    return json.dumps(vector)


def deserialize(blob):
    """The `memory.embedding` column's TEXT -> an embedding (list[float]),
    or None if `blob` is empty/unparsable - e.g. a row saved before this
    column existed, or saved while embeddings were unavailable."""
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None

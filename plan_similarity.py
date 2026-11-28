"""Find an ontology plan the model already built for a question worded a little differently.

Variations of the same question usually resolve to the same concepts and domains. This keeps the embeddings of recent
questions with the plans built for them, so a new question can be compared with them. The embedding comes from the same
Azure OpenAI deployment the search app uses. Vectors are cut to their first 256 dimensions and re-normalised (the
text-embedding-3 models are trained so a prefix stays meaningful) to keep the comparison cheap without numpy.

A similarity is a distance, not a probability. Run in shadow first (MCP_PLAN_REUSE_SIMILARITY=0): each question that goes to
the model logs its nearest earlier question's similarity and whether the model chose the same concepts, which shows what
agreement a given similarity really buys. Reuse switches on only when a threshold is set.
"""
import math
import threading
import time

import requests

DIMS = 256


def prefix_unit(vector, dims=DIMS):
    cut = [float(x) for x in vector[:dims]]
    norm = math.sqrt(sum(x * x for x in cut)) or 1.0
    return [x / norm for x in cut]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    """One Azure OpenAI embeddings call. Off (returns None) unless endpoint, key and deployment are all set; any failure
    returns None so the caller carries on with the model."""

    def __init__(self, endpoint, key, deployment, dimensions=1536, timeout=2.0, http=requests):
        self.endpoint, self.key, self.deployment = (endpoint or "").rstrip("/"), key or "", deployment or ""
        self.dimensions, self.timeout, self._http = dimensions, timeout, http
        self._down_until = 0.0

    @property
    def enabled(self):
        return bool(self.endpoint and self.key and self.deployment)

    def embed(self, text):
        if not self.enabled or time.monotonic() < self._down_until:
            return None
        try:
            reply = self._http.post(f"{self.endpoint}/openai/v1/embeddings", headers={"api-key": self.key},
                                    json={"model": self.deployment, "input": text, "dimensions": self.dimensions},
                                    timeout=self.timeout)
            reply.raise_for_status()
            return prefix_unit(reply.json()["data"][0]["embedding"])
        except Exception:
            self._down_until = time.monotonic() + 30
            return None


class SimilarPlans:
    """Recent (embedding, plan) pairs, newest kept, grouped by a scope (ontology version, model, conversation context)."""

    def __init__(self, max_entries=1000):
        self._max, self._lock, self._entries = max_entries, threading.Lock(), []

    def nearest(self, scope, vector):
        """(similarity, plan) of the closest stored question in this scope, or (0.0, None)."""
        best, plan = 0.0, None
        with self._lock:
            entries = list(self._entries)
        for entry_scope, entry_vector, entry_plan in entries:
            if entry_scope != scope:
                continue
            similarity = cosine(vector, entry_vector)
            if similarity > best:
                best, plan = similarity, entry_plan
        return best, plan

    def add(self, scope, vector, plan):
        with self._lock:
            self._entries.append((scope, vector, plan))
            if len(self._entries) > self._max:
                del self._entries[: len(self._entries) - self._max]

    def __len__(self):
        return len(self._entries)


def reusable(plan):
    """A plan another question may borrow: the model resolved it to concepts, with no clarification and no degraded route."""
    resolution = (plan or {}).get("resolution") or {}
    return bool(resolution.get("matched_concepts") and resolution.get("resolved_by") == "model"
                and not resolution.get("needs_clarification")
                and not (resolution.get("feelings_route") or {}).get("degraded")
                and not (plan or {}).get("clarification"))

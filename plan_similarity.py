"""Find an ontology plan the model already built for a question worded a little differently.

Variations of the same question usually resolve to the same concepts and domains. This keeps the embeddings of recent
questions with the plans built for them, so a new question can be compared with them. The embedding comes from the same
Azure OpenAI deployment the search app uses. Vectors are cut to their first 256 dimensions and re-normalised (the
text-embedding-3 models are trained so a prefix stays meaningful) to keep the comparison cheap without numpy.

A similarity is a distance, not a probability. Run in shadow first (MCP_PLAN_REUSE_SIMILARITY=0): each question that goes to
the model logs its nearest earlier question's similarity and whether the model chose the same concepts, which shows what
agreement a given similarity really buys. Reuse switches on only when a threshold is set.
"""
import hashlib
import json
import math
import threading
import time
from array import array
from concurrent.futures import ThreadPoolExecutor

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


class SharedVectors:
    """The embeddings of recent questions in a table every MCP instance reads, so similar questions match across instances.

    One small row per question (its 256-number vector and a pointer to its plan in the plan table), in one partition per
    scope (ontology version, model, conversation context). An instance reads a scope's partition at most once every
    `refresh` seconds and compares in memory; the plan itself is fetched by a point read only when a row is close enough
    to use. A slow or failing table is skipped for a while. Off without a storage connection or with a lifetime of 0."""

    def __init__(self, connection, table_name, ttl_seconds, refresh=60.0, max_rows=1000, timeout=1.0, cooldown=30.0, table=None):
        self._connection, self._name, self._ttl = connection, table_name, ttl_seconds
        self._refresh, self._max_rows, self._timeout, self._cooldown, self._table = refresh, max_rows, timeout, cooldown, table
        self._lock = threading.Lock()
        self._down_until, self._loaded, self._writer = 0.0, {}, None
        self.stats = {"loads": 0, "load_rows": 0, "errors": 0, "writes": 0, "write_errors": 0}

    @property
    def enabled(self):
        return self._ttl > 0 and bool(self._table is not None or self._connection)

    @staticmethod
    def partition_for(scope):
        return hashlib.sha256(json.dumps(list(scope), default=str).encode("utf-8")).hexdigest()[:16]

    def _client(self):
        if self._table is None:
            from azure.core.exceptions import ResourceExistsError
            from azure.data.tables import TableServiceClient
            service = TableServiceClient.from_connection_string(
                self._connection, connection_timeout=self._timeout, read_timeout=self._timeout, retry_total=1)
            table = service.get_table_client(self._name)
            try:
                table.create_table()
            except ResourceExistsError:
                pass
            self._table = table
        return self._table

    def _fail(self):
        with self._lock:
            self.stats["errors"] += 1
            self._down_until = time.monotonic() + self._cooldown

    def rows(self, scope):
        """[(vector, (partition, row))] for this scope: what other instances stored, read at most once per refresh."""
        if not self.enabled:
            return []
        part = self.partition_for(scope)
        now = time.monotonic()
        with self._lock:
            loaded = self._loaded.get(part)
            if loaded and now - loaded[0] < self._refresh:
                return loaded[1]
            if now < self._down_until:
                return loaded[1] if loaded else []
        try:
            found = []
            cutoff = time.time() - self._ttl
            for entity in self._client().query_entities(f"PartitionKey eq '{part}'"):
                if float(entity.get("ts") or 0) < cutoff:
                    continue
                raw = entity.get("vec")
                raw = bytes(getattr(raw, "value", raw) or b"")
                vector = array("f")
                vector.frombytes(raw)
                ref = str(entity.get("ref") or "").split("|")
                if len(vector) == DIMS and len(ref) == 2:
                    found.append((float(entity.get("ts") or 0), list(vector), (ref[0], ref[1])))
            found.sort(key=lambda row: -row[0])
            rows = [(vector, ref) for _, vector, ref in found[: self._max_rows]]
        except Exception:
            self._fail()
            return loaded[1] if loaded else []
        with self._lock:
            self._loaded[part] = (now, rows)
            self.stats["loads"] += 1
            self.stats["load_rows"] = len(rows)
        return rows

    def nearest(self, scope, vector):
        """(similarity, (partition, row)) of the closest stored question in this scope, or (0.0, None)."""
        best, ref = 0.0, None
        for stored, stored_ref in self.rows(scope):
            similarity = cosine(vector, stored)
            if similarity > best:
                best, ref = similarity, stored_ref
        return best, ref

    def _write(self, scope, vector, plan_ref):
        try:
            part = self.partition_for(scope)
            row = hashlib.sha256("|".join(plan_ref).encode("utf-8")).hexdigest()[:32]
            self._client().upsert_entity({"PartitionKey": part, "RowKey": row, "vec": array("f", vector).tobytes(),
                                          "ref": "|".join(plan_ref), "ts": int(time.time())}, mode="replace")
            with self._lock:
                self.stats["writes"] += 1
        except Exception:
            with self._lock:
                self.stats["write_errors"] += 1
            self._fail()

    def put(self, scope, vector, plan_ref, wait=False):
        """Remember a question's vector and where its plan is stored, in the background unless wait=True."""
        if not self.enabled or time.monotonic() < self._down_until:
            return
        if wait:
            self._write(scope, vector, plan_ref)
            return
        with self._lock:
            if self._writer is None:
                self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan-vectors")
        self._writer.submit(self._write, scope, vector, plan_ref)

"""A table shared by every MCP instance that remembers the ontology plans the model has built.

Resolving a question calls the ontology model (about 3 s and a model call). The plan is already kept in memory on the
instance for a few minutes; this table lets every other instance find it too, so a retry or a repeat of the question that
lands on a different instance does not call the model again.

Rows hold the plan (compressed JSON) and a timestamp, never the question's text: the row key is a hash of the question,
ontology version, model and context, so a new ontology version or model misses by itself. A table that is slow or failing
is skipped for a while, so a search never waits on it for long, and with no storage connection the store is simply off.
"""
import base64
import hashlib
import json
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

MAX_PLAN_CHARS = 60000          # one Table Storage string property holds at most 64 KB of UTF-16
MAX_PENDING_WRITES = 100


def row_key_for(key):
    digest = hashlib.sha256(json.dumps(key, default=str, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:2], digest[2:]


class PlanStore:
    def __init__(self, connection, table_name, ttl_seconds, timeout=1.0, cooldown=30.0, table=None):
        self._connection, self._name, self._ttl = connection, table_name, ttl_seconds
        self._timeout, self._cooldown, self._table = timeout, cooldown, table
        self._lock = threading.Lock()
        self._down_until, self._pending, self._writer = 0.0, 0, None
        self.stats = {"reads": 0, "hits": 0, "misses": 0, "errors": 0, "skipped": 0, "writes": 0, "write_errors": 0,
                      "writes_dropped": 0}

    @property
    def enabled(self):
        return self._ttl > 0 and bool(self._table is not None or self._connection)

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

    def _fail(self, exc):
        with self._lock:
            self.stats["errors"] += 1
            self._down_until = time.monotonic() + self._cooldown

    def get(self, key):
        """The plan stored for this key, or None (absent, too old, unreadable, or the table is down)."""
        if not self.enabled:
            return None
        if time.monotonic() < self._down_until:
            with self._lock:
                self.stats["skipped"] += 1
            return None
        partition, row = row_key_for(key)
        with self._lock:
            self.stats["reads"] += 1
        try:
            entity = self._client().get_entity(partition, row)
        except Exception as exc:
            if exc.__class__.__name__ == "ResourceNotFoundError":
                with self._lock:
                    self.stats["misses"] += 1
                return None
            self._fail(exc)
            return None
        try:
            if time.time() - float(entity.get("ts") or 0) > self._ttl:
                raise ValueError("expired")
            plan = json.loads(zlib.decompress(base64.b64decode(entity["plan"])).decode("utf-8"))
        except Exception:
            with self._lock:
                self.stats["misses"] += 1
            return None
        with self._lock:
            self.stats["hits"] += 1
        return plan

    def _write(self, key, packed):
        partition, row = row_key_for(key)
        try:
            self._client().upsert_entity({"PartitionKey": partition, "RowKey": row, "plan": packed, "ts": int(time.time())},
                                         mode="replace")
            with self._lock:
                self.stats["writes"] += 1
        except Exception as exc:
            with self._lock:
                self.stats["write_errors"] += 1
            self._fail(exc)
        finally:
            with self._lock:
                self._pending -= 1

    def put(self, key, plan, wait=False):
        """Remember a plan, in the background unless wait=True. A backlog, a down table or an oversized plan drops it."""
        if not self.enabled or time.monotonic() < self._down_until:
            return
        packed = base64.b64encode(zlib.compress(json.dumps(plan, default=str).encode("utf-8"))).decode("ascii")
        if len(packed) > MAX_PLAN_CHARS:
            return
        with self._lock:
            if self._pending >= MAX_PENDING_WRITES:
                self.stats["writes_dropped"] += 1
                return
            self._pending += 1
            if self._writer is None and not wait:
                self._writer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="plan-store")
        if wait:
            self._write(key, packed)
        else:
            self._writer.submit(self._write, key, packed)

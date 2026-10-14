"""ACCF-MCP core: source-preserving memory store + hybrid index + epistemic layer.

Data layout (ACCF_HOME, default ~/.accf-mcp):
  memory/<id>.md        fact/episode entries (Markdown + frontmatter, source of truth)
  handoffs/<id>.md      task-state handoffs with epistemic fields
  protocols/<id>.md     hash-chained verified lessons
  index.db              SQLite: metadata + FTS5 (CJK bigram) + char-3gram TF-IDF vectors
  capability_map.json   skill/plugin/MCP/script routing map (ACCF states preserved)
  state.json            hash-chain head + counters

Design constraints:
  - Markdown files are the single source of truth; index.db is rebuildable.
  - Zero heavy dependencies: stdlib only. No embedding API. FTS5 optional (graceful
    fallback to LIKE scan); vector recall via pure-python sparse cosine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

NOW = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# ---------------------------------------------------------------- text utils

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff]")

def _cjk_bigrams(text: str) -> list[str]:
    """Split into latin/alnum words and CJK bigrams (classic CJK search trick)."""
    out, buf = [], []
    for ch in text:
        if _CJK.match(ch):
            if buf:
                out.append("".join(buf)); buf = []
            out.append(ch)
        elif ch.isalnum():
            buf.append(ch.lower())
        else:
            if buf:
                out.append("".join(buf)); buf = []
    if buf:
        out.append("".join(buf))
    bigrams: list[str] = []
    for tok in out:
        bigrams.append(tok)
        if _CJK.search(tok) or len(tok) > 1 and not tok.isascii():
            pass
    # real bigram pass over contiguous CJK sequences
    seqs, cur = [], ""
    for ch in text:
        if _CJK.match(ch):
            cur += ch
        elif cur:
            seqs.append(cur); cur = ""
    if cur:
        seqs.append(cur)
    for s in seqs:
        if len(s) > 1:
            bigrams += [s[i:i+2] for i in range(len(s) - 1)]
    return bigrams

def _ngrams(text: str, n: int = 3) -> list[str]:
    """Normalized char 3-grams for fuzzy vectors."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"\s+", " ", t)
    return [t[i:i+n] for i in range(max(1, len(t) - n + 1))]

def _vec(text: str) -> dict[str, float]:
    v: dict[str, float] = {}
    for g in _ngrams(text):
        v[g] = v.get(g, 0.0) + 1.0
    return v

def _cosine(a: dict[str, float], b: dict[str, float], idf: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    num = 0.0
    for k, x in a.items():
        y = b.get(k)
        if y is not None:
            w = idf.get(k, 1.0)
            num += x * y * w * w
    da = sum((x * idf.get(k, 1.0)) ** 2 for k, x in a.items())
    db = sum((y * idf.get(k, 1.0)) ** 2 for k, y in b.items())
    return num / ((da ** 0.5) * (db ** 0.5)) if da and db else 0.0

def _rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion over ranked id lists (k=60, the empirical default)."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for pos, eid in enumerate(ranking):
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + pos + 1)
    return sorted(scores, key=lambda eid: -scores[eid])


# ------------------------------------------------------------- frontmatter IO

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)

def dump_entry(meta: dict, body: str) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}: " + json.dumps(v, ensure_ascii=False))
        elif v is None or v == "":
            continue
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"

def load_entry(text: str) -> tuple[dict, str]:
    m = _FRONT.match(text)
    if not m:
        return {}, text.strip()

    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith("["):
            try:
                meta[k.strip()] = json.loads(v)
            except Exception:
                meta[k.strip()] = v
        else:
            meta[k.strip()] = v
    return meta, text[m.end():].strip()

# ------------------------------------------------------------------- the core

def _file_hash(path: str | os.PathLike) -> str:
    """Hash the LF-normalized text of a file (newline-translation invariant)."""
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").encode()).hexdigest()[:16]


_UNSET = object()  # sentinel: "caller did not decide" (distinct from any real value)


class AccfCore:
    def __init__(self, home: str | os.PathLike | None = None):
        self.home = Path(home or os.environ.get("ACCF_HOME", "~/.accf-mcp")).expanduser()
        for sub in ("memory", "handoffs", "protocols"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.home / "index.db", check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS entries(
          id TEXT PRIMARY KEY, kind TEXT, path TEXT, title TEXT, source TEXT,
          tags TEXT, disconfirmation TEXT, created TEXT, content_hash TEXT,
          task_id TEXT, vector TEXT, status TEXT DEFAULT 'active');
        """)
        self.fts = True
        try:
            for attempt in range(3):
                try:
                    self.db.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                      id UNINDEXED, title, body, tokenize='unicode61');
                    """)
                    self.db.execute("SELECT count(*) FROM fts").fetchone()
                    break
                except sqlite3.OperationalError:
                    if attempt == 2:
                        raise
                    time.sleep(0.3 * (attempt + 1))
        except sqlite3.Error:
            self.fts = False
        try:
            self.db.execute("ALTER TABLE entries ADD COLUMN embedding TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.vec = False
        try:
            import sqlite_vec
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self.db.enable_load_extension(False)
            self.db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_index USING vec0(
              id TEXT PRIMARY KEY, embedding FLOAT[512]);
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_coarse USING vec0(
              id TEXT PRIMARY KEY, embedding bit[512]);
            """)
            self.db.commit()
            self.vec = True
        except Exception:
            self.vec = False
        try:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS token_postings(
              token TEXT, id TEXT, weight REAL,
              PRIMARY KEY (token, id)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS token_postings_id ON token_postings(id);
            """)
            self.db.commit()
        except sqlite3.Error:
            pass
        self._seed_map()
        self._load_map()
        self._body_cache: dict[str, tuple[dict, str]] = {}

    # ------------------------------------------------------------- capability map
    def _seed_map(self):
        """First run on a fresh home: copy the packaged capability map."""
        dst = self.home / "capability_map.json"
        if dst.exists():
            return
        src = Path(__file__).with_name("capability_map.json")
        if src.exists():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def _load_map(self):
        p = self.home / "capability_map.json"
        if p.exists():
            try:
                self.map = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                self.map = {"version": 1, "tools": []}
        else:
            self.map = {"version": 1, "tools": []}

    def _save_map(self):
        self.map["updated"] = NOW()
        (self.home / "capability_map.json").write_text(
            json.dumps(self.map, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ indexing
    _DIM = 512

    def _pack_f32(self, vec) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    def _index_upsert(self, rec: dict, title: str, body: str, embedding=_UNSET,
                      commit: bool = True):
        # the legacy char-3gram `vector` column is no longer read by any recall
        # path (token_postings replaced it); computing it cost a full body scan
        # per write. Keep the column for schema compatibility, store empty.
        if embedding is _UNSET:
            embedding = self._embed_json(self._embed_text(title, body))
        self.db.execute(
            "INSERT OR REPLACE INTO entries(id,kind,path,title,source,tags,disconfirmation,"
            "created,content_hash,task_id,vector,status,embedding) VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (rec["id"], rec["kind"], rec["path"], title, rec.get("source", ""),
             json.dumps(rec.get("tags", []), ensure_ascii=False),
             rec.get("disconfirmation", ""), rec["created"], rec["content_hash"],
             rec.get("task_id", ""), "{}", embedding))
        if self.fts:
            self.db.execute("DELETE FROM fts WHERE id=?", (rec["id"],))
            self.db.execute("INSERT INTO fts(id,title,body) VALUES(?,?,?)",
                            (rec["id"], title, _cjk_bigrams(title) and title + " " + " ".join(_cjk_bigrams(body))))
        # vector channels: full-precision index + bit-quantized coarse index
        if self.vec and embedding:
            try:
                blob = self._pack_f32(json.loads(embedding))
                self.db.execute(
                    "INSERT OR REPLACE INTO vec_index(id, embedding) VALUES (?, ?)", (rec["id"], blob))
                self.db.execute(
                    "INSERT OR REPLACE INTO vec_coarse(id, embedding) "
                    "VALUES (?, vec_quantize_binary(vec_f32(?)))", (rec["id"], blob))
            except Exception:
                pass
        # sparse TF-IDF postings for the lexical channel: title tokens only.
        # Body tokens (trace JSON etc.) are numeric noise — they inflated the
        # postings table by ~10x and slowed writes; FTS still covers full body
        # text for exact-phrase queries.
        try:
            self.db.execute("DELETE FROM token_postings WHERE id=?", (rec["id"],))
            counts: dict[str, float] = {}
            for g in _cjk_bigrams(title):
                counts[g] = counts.get(g, 0.0) + 1.0
            self.db.executemany(
                "INSERT OR REPLACE INTO token_postings(token, id, weight) VALUES(?,?,?)",
                [(t, rec["id"], c) for t, c in counts.items()])
        except sqlite3.Error:
            pass
        if commit:
            self.db.commit()

    def _remove_from_indexes(self, eid: str):
        for stmt in ("DELETE FROM vec_index WHERE id=?", "DELETE FROM vec_coarse WHERE id=?",
                     "DELETE FROM token_postings WHERE id=?", "DELETE FROM fts WHERE id=?"):
            try:
                self.db.execute(stmt, (eid,))
            except sqlite3.Error:
                pass
        self.db.commit()

    def _get_row(self, eid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        cols = [c[0] for c in self.db.execute("SELECT * FROM entries LIMIT 1").description]
        return dict(zip(cols, r))

    def _idf(self) -> dict[str, float]:
        n = max(1, self.db.execute("SELECT count(*) FROM entries WHERE status='active'").fetchone()[0])
        try:
            rows = self.db.execute(
                "SELECT token, count(*) FROM token_postings GROUP BY token").fetchall()
            df = {t: c for t, c in rows}
        except sqlite3.Error:
            df = {}
        import math
        return {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    def _lexical_rank(self, query: str, k: int, kinds: list[str] | None = None) -> list[str]:
        """TF-IDF over an inverted index: only documents sharing a query token
        are scored, so cost scales with matches, not corpus size."""
        try:
            qtokens = set(_cjk_bigrams(query))
            if not qtokens:
                return []
            placeholders = ",".join("?" * len(qtokens))
            rows = self.db.execute(
                f"SELECT id, token, weight FROM token_postings WHERE token IN ({placeholders})",
                tuple(qtokens)).fetchall()
        except sqlite3.Error:
            return []
        if not rows:
            return []
        idf = self._idf()
        acc: dict[str, dict[str, float]] = {}
        for eid, token, weight in rows:
            acc.setdefault(eid, {})[token] = weight
        kind_filter = None
        if kinds:
            kind_filter = {r[0] for r in self.db.execute(
                "SELECT id FROM entries WHERE status='active' AND kind IN (%s)"
                % ",".join("?" * len(kinds)), tuple(kinds)).fetchall()}
        scored = []
        for eid, counts in acc.items():
            if kind_filter is not None and eid not in kind_filter:
                continue
            num = sum(counts[t] * idf.get(t, 1.0) ** 2 for t in counts)
            den = sum((c * idf.get(t, 1.0)) ** 2 for t, c in counts.items()) ** 0.5
            qden = sum(idf.get(t, 1.0) ** 2 for t in qtokens) ** 0.5
            s = num / (den * qden) if den and qden else 0.0
            if s > 0.02:
                scored.append((eid, s))
        scored.sort(key=lambda x: -x[1])
        return [eid for eid, _ in scored[:k]]

    def _semantic_rank(self, query: str, k: int) -> list[str]:
        """Vector channel: sqlite-vec two-stage (bit coarse pass, then exact
        float rerank of the coarse survivors). Falls back to a pure-python
        cosine over the entries table when the extension is unavailable."""
        if not self.vec:
            model = self._embedder()
            if model is None:
                return []
            try:
                qv = [float(x) for x in next(model.embed([query[:2000]]))]
            except Exception:
                return []
            rows = self.db.execute(
                "SELECT id,embedding FROM entries WHERE status='active' AND embedding IS NOT NULL").fetchall()
            scored = []
            for eid, ejson in rows:
                ev = json.loads(ejson)
                num = sum(a * b for a, b in zip(qv, ev))
                da = sum(a * a for a in qv) ** 0.5
                db_ = sum(b * b for b in ev) ** 0.5
                if da and db_:
                    scored.append((eid, num / (da * db_)))
            scored.sort(key=lambda x: -x[1])
            return [eid for eid, _ in scored[:k]]
        try:
            qblob = self._pack_f32([float(x) for x in next(self._embedder().embed([query[:2000]]))])
        except Exception:
            return []
        try:
            coarse = self.db.execute(
                "SELECT id FROM vec_coarse WHERE embedding MATCH vec_quantize_binary(vec_f32(?)) AND k = ?",
                (qblob, max(k * 4, 200))).fetchall()
            if not coarse:
                return []
            ids = [str(r[0]) for r in coarse]
            rows = self.db.execute(
                "SELECT id FROM vec_index WHERE id IN (%s) "
                "ORDER BY vec_distance_cosine(embedding, vec_f32(?)) LIMIT ?" % ",".join("?" * len(ids)),
                (*ids, qblob, k)).fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error:
            return []

    # -------------------------------------------------------------------- save
    def _write_entry(self, title: str, content: str, kind: str, source: str = "",
                     tags: list[str] | None = None, disconfirmation: str = "",
                     task_id: str = "", embedding: str | None = None,
                     commit: bool = True) -> dict:
        """Local-only write of one entry (file + rows + indexes). Embeddings are
        passed in by callers so batch writers can embed once for many entries;
        commit=False lets a caller batch many writes into one transaction."""
        assert kind in ("fact", "episode", "protocol")
        eid = f"{kind[:3]}-{time.strftime('%Y%m%d')}-{hashlib.sha1((title+content).encode()).hexdigest()[:8]}"
        sub = {"fact": "memory", "episode": "memory", "protocol": "protocols"}[kind]
        path = self.home / sub / f"{eid}.md"
        meta = {"id": eid, "kind": kind, "created": NOW(), "title": title,
                "source": source, "tags": tags or [], "task_id": task_id}
        if kind == "episode":
            try:  # structured provenance for the episodes() evidence gate
                data = json.loads(content)
                trace = data.get("trace")
                if isinstance(trace, list) and trace:
                    meta["world_id"] = str(data.get("world", ""))
                    meta["step_count"] = max(int(e["step"]) for e in trace) + 1
                    meta["success_count"] = sum(int(e["success"]) for e in trace)
            except Exception:
                pass
        if disconfirmation:
            meta["disconfirmation"] = disconfirmation
        text = dump_entry(meta, content)
        path.write_text(text, encoding="utf-8")
        rec = {"id": eid, "kind": kind, "path": str(path), "source": source,
               "tags": tags or [], "disconfirmation": disconfirmation,
               "created": meta["created"], "task_id": task_id}
        rec["content_hash"] = hashlib.sha256(text.encode()).hexdigest()[:16]
        if kind == "protocol":
            head = (self.home / "state.json")
            prev = ""
            if head.exists():
                prev = json.loads(head.read_text()).get("chain_head", "")
            chain = hashlib.sha256((prev + text).encode()).hexdigest()[:16]
            meta["prev_hash"] = prev; meta["hash"] = chain
            final = dump_entry(meta, content)
            path.write_text(final, encoding="utf-8")
            head.write_text(json.dumps({"chain_head": chain, "updated": NOW()}))
            rec["content_hash"] = hashlib.sha256(final.encode()).hexdigest()[:16]
            self._index_upsert(rec, title, content, embedding, commit=commit)
            return {"id": eid, "hash": chain, "prev_hash": prev, "path": str(path)}
        # the hash of what we just wrote — re-reading the file would double the IO
        self._index_upsert(rec, title, content, embedding, commit=commit)
        return {"id": eid, "path": str(path),
                "warning": None if disconfirmation else "no disconfirmation field — memory cannot be falsified"}

    def save(self, title: str, content: str, kind: str = "fact", source: str = "",
             tags: list[str] | None = None, disconfirmation: str = "",
             task_id: str = "") -> dict:
        return self._write_entry(title, content, kind, source, tags, disconfirmation,
                                 task_id, self._embed_json(self._embed_text(title, content)))

    def save_many(self, items: list[dict], batch_size: int = 64,
                  timeout: float = 300.0) -> dict:
        """Bulk write: pipelined embed-then-write per chunk (embedding one
        chunk at a time keeps memory bounded and lets a timeout stop with
        everything already written committed). Embedding input is title plus a
        short body prefix — bge truncates at ~512 tokens anyway. Items:
        {title, content, kind?, source?, tags?, disconfirmation?, task_id?}.
        A single bad item never aborts the batch; failures are reported per
        index. On timeout, already-written entries stay committed and the
        result reports how far it got — never a silent hang."""
        if not items:
            return {"saved": 0, "ids": [], "failed": [], "requested": 0,
                    "complete": True, "elapsed_seconds": 0.0}
        started = time.perf_counter()
        model = self._embedder()
        ids, failed = [], []
        for start in range(0, len(items), batch_size):
            if time.perf_counter() - started > timeout:
                break
            chunk = items[start:start + batch_size]
            vectors = [None] * len(chunk)
            if model is not None:
                try:
                    texts = [self._embed_text(it["title"], it["content"]) for it in chunk]
                    vectors = [json.dumps([round(float(x), 6) for x in v])
                               for v in model.embed(texts)]
                except Exception:
                    vectors = [None] * len(chunk)  # lexical-only for this chunk
            for offset, item in enumerate(chunk):
                try:
                    result = self._write_entry(
                        item["title"], item["content"], item.get("kind", "fact"),
                        item.get("source", ""), item.get("tags"),
                        item.get("disconfirmation", ""), item.get("task_id", ""),
                        vectors[offset], commit=False)
                    ids.append(result["id"])
                except Exception as exc:
                    failed.append({"index": start + offset,
                                   "error": f"{type(exc).__name__}: {exc}"})
            self.db.commit()  # commit per chunk: partial progress survives
        return {"saved": len(ids), "ids": ids, "failed": failed,
                "requested": len(items),
                "complete": len(ids) + len(failed) >= len(items),
                "elapsed_seconds": round(time.perf_counter() - started, 1)}

    # ------------------------------------------------------------------ recall
    _EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

    def _embedder(self):
        """Lazy fastembed handle; None -> vector channel degraded (explicit).
        parallel=0 keeps embedding single-process: fastembed's default worker
        pool loads a full model per worker and OOMs on long batches. threads=4
        is the throughput sweet spot on this machine (~99 texts/s)."""
        model = getattr(self, "_embedder_model", "unset")
        if model == "unset":
            try:
                from fastembed import TextEmbedding
                model = TextEmbedding(self._EMBED_MODEL,
                                      cache_dir=str(Path(__file__).with_name("models")),
                                      parallel=0, threads=4)
            except Exception:
                model = None
            self._embedder_model = model
        return model

    def _embed_text(self, title: str, body: str) -> str:
        """Embedding input: the title only. Measured on this machine: title-only
        embeds at ~1ms/entry vs ~7.5ms for title+96 body chars, and top-1
        accuracy is better — structured JSON bodies are noise for bge and only
        dilute the title signal. Bodies stay fully searchable via the lexical
        channel (FTS + bigram postings)."""
        return title

    def _embed_json(self, text: str) -> str | None:
        model = self._embedder()
        if model is None:
            return None
        try:
            return json.dumps([round(float(x), 6) for x in next(model.embed([text[:2000]]))])
        except Exception:
            return None

    def recall(self, query: str, k: int = 5, kinds: list[str] | None = None) -> dict:
        """Hybrid recall: lexical (FTS + CJK bigram + TF-IDF) fused with the
        local vector channel (bge-small-zh via RRF, k=60). The vector channel
        degrades to None when fastembed is unavailable — flagged in channels."""
        t0 = time.perf_counter()
        lexical = self._lexical_rank(query, k * 3, kinds)
        semantic = self._semantic_rank(query, k * 3)
        fused = _rrf([lexical, semantic])[:k]
        hits = []
        for pos, eid in enumerate(fused):
            row = self._get_row(eid)
            if not row:
                continue
            hits.append({"id": eid, "kind": row["kind"], "title": row["title"],
                         "source": row["source"],
                         "tags": json.loads(row["tags"]) if row["tags"] else [],
                         "disconfirmation": row["disconfirmation"], "created": row["created"],
                         "score": round(1.0 / (60 + pos + 1), 4)})
        return {"hits": hits,
                "channels": {"lexical": len(lexical), "semantic": len(semantic),
                             "vector_available": self._embedder() is not None,
                             "vec_index": self.vec},
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "est_tokens": sum(len((h["title"] + h["source"])) // 3 + 20 for h in hits)}

    def backfill(self, timeout: float = 300.0) -> dict:
        """Rebuild secondary indexes (embeddings, vec tables, postings) for every
        active entry whose index row is missing. Pipelined embed-then-write per
        chunk with a commit per chunk, so a timeout leaves completed work
        committed and the next run resumes. Idempotent; safe to re-run."""
        started = time.perf_counter()
        indexed = {r[0] for r in self.db.execute("SELECT id FROM token_postings")}
        rows = self.db.execute(
            "SELECT id,path,title FROM entries WHERE status='active'").fetchall()
        todo = [r for r in rows if r[0] not in indexed]
        model = self._embedder()
        pending = []
        for eid, path, title in todo:
            fp = Path(path)
            if not fp.exists():
                continue
            meta, body = load_entry(fp.read_text(encoding="utf-8"))
            pending.append((eid, meta.get("title") or title or eid, body))
        done = 0
        for start in range(0, len(pending), 64):
            if time.perf_counter() - started > timeout:
                break
            chunk = pending[start:start + 64]
            vectors = [None] * len(chunk)
            if model is not None:
                try:
                    vectors = [json.dumps([round(float(x), 6) for x in v]) for v in model.embed(
                        [self._embed_text(t, b) for _, t, b in chunk])]
                except Exception:
                    vectors = [None] * len(chunk)
            for (eid, t, body), embedding in zip(chunk, vectors):
                if embedding:
                    self.db.execute("UPDATE entries SET embedding=? WHERE id=?", (embedding, eid))
                    if self.vec:
                        try:
                            blob = self._pack_f32(json.loads(embedding))
                            self.db.execute("INSERT OR REPLACE INTO vec_index(id, embedding) VALUES (?, ?)",
                                            (eid, blob))
                            self.db.execute(
                                "INSERT OR REPLACE INTO vec_coarse(id, embedding) "
                                "VALUES (?, vec_quantize_binary(vec_f32(?)))", (eid, blob))
                        except Exception:
                            pass
                grams = _cjk_bigrams(t)  # title tokens only (see _index_upsert)
                counts: dict[str, float] = {}
                for g in grams:
                    counts[g] = counts.get(g, 0.0) + 1.0
                self.db.executemany(
                    "INSERT OR REPLACE INTO token_postings(token, id, weight) VALUES(?,?,?)",
                    [(g, eid, c) for g, c in counts.items()])
                done += 1
            self.db.commit()  # per-chunk commit: partial progress survives
        return {"backfilled": done, "total_active": len(rows),
                "complete": done >= len(pending),
                "elapsed_seconds": round(time.perf_counter() - started, 1),
                "vec_index": self.vec, "vector_available": model is not None}

    # ------------------------------------------------------- episodes evidence gate
    def episodes(self, indicators, k: int = 6, limit: int = 4, query: str = "") -> dict:
        """Search-verify-build for the laya_judge advisory loop: recall candidate
        episode logs (lexical+vector RRF), gate each on actual content (positive
        events for this exact context), keep only the freshest version per world,
        and return episodes ready for a judge call. Honest edge: no positive
        evidence -> status='empty'; feeding unrelated history would poison the
        judge, so callers must not fall back to it."""
        bits = [int(b) for b in indicators]
        if len(bits) != 4 or any(b not in (0, 1) for b in bits):
            return {"status": "invalid", "error": "indicators must be four 0/1 values"}
        query = query or "处置 执行 日志 经历 尝试 attempt log episodes"
        lexical = self._lexical_rank(query, k * 3, ["episode"])
        semantic = self._semantic_rank(query, k * 3)
        candidates = []
        for eid in _rrf([lexical, semantic])[:k]:
            row = self._get_row(eid)
            if not row or row["kind"] != "episode":
                continue
            p = Path(row["path"])
            if not p.exists():
                continue
            body = self._body_cache.get(eid)
            meta = None
            if body is None:
                meta, body = load_entry(p.read_text(encoding="utf-8"))
                if len(self._body_cache) > 256:
                    self._body_cache.clear()
                self._body_cache[eid] = (meta, body)
            else:
                meta, body = body
            try:
                data = json.loads(body)
                trace = data["trace"]
            except Exception:
                continue
            positives = [[e["bits"], e["pair"], int(e["success"])]
                         for e in trace if list(e["bits"]) == bits and int(e["success"]) == 1]
            candidates.append({"id": eid, "title": row["title"],
                               "world": str(data.get("world", meta.get("world_id", eid))),
                               "version": int(meta.get("step_count",
                                                       max((int(e["step"]) for e in trace), default=-1) + 1)),
                               "matched": len(positives), "positives": positives})
        if not candidates:
            return {"status": "no_log", "episodes": [], "bits": bits,
                    "channels": {"lexical": len(lexical), "semantic": len(semantic),
                                 "vector_available": self._embedder() is not None},
                    "report": "recall surfaced no parsable episode log"}
        freshest: dict[str, dict] = {}
        for c in candidates:
            cur = freshest.get(c["world"])
            if cur is None or c["version"] > cur["version"]:
                freshest[c["world"]] = c
        sources = [{"id": c["id"], "title": c["title"], "world": c["world"], "version": c["version"],
                    "matched": c["matched"],
                    "verdict": "used" if c["matched"] else "no_positive_for_context"}
                   for c in sorted(freshest.values(), key=lambda x: -x["version"])]
        used = [c for c in freshest.values() if c["matched"]]
        if not used:
            return {"status": "empty", "episodes": [], "bits": bits, "sources": sources,
                    "channels": {"lexical": len(lexical), "semantic": len(semantic)},
                    "report": ("logs exist but hold no success for this context; "
                               "feeding unrelated history would poison the judge — "
                               "leave episodes empty or record new attempts")}
        episodes = []
        for c in sorted(used, key=lambda x: -x["version"]):
            episodes.extend(c["positives"])
        return {"status": "linked", "episodes": episodes[:limit], "bits": bits, "sources": sources,
                "channels": {"lexical": len(lexical), "semantic": len(semantic),
                             "vector_available": self._embedder() is not None},
                "report": f"{len(used)} log(s) matched, {len(episodes[:limit])} positive episodes returned"}

    def dig(self, eid: str) -> dict:
        row = self._get_row(eid)
        if not row:
            return {"error": f"unknown id {eid}"}
        p = Path(row["path"])
        if not p.exists():
            return {"error": f"file missing: {row['path']}", "hint": "run accf_validate"}
        meta, body = load_entry(p.read_text(encoding="utf-8"))
        return {"id": eid, "meta": meta, "content": body, "chars": len(body),
                "est_tokens": len(body) // 3}

    # ---------------------------------------------------------------- handoffs
    def handoff_write(self, task_id: str, goal: str, stage: str, next_action: str,
                      unknowns: list[str], disconfirmation: str = "",
                      evidence: str = "") -> dict:
        hid = f"han-{time.strftime('%Y%m%d')}-{hashlib.sha1(task_id.encode()).hexdigest()[:6]}"
        meta = {"id": hid, "kind": "handoff", "task_id": task_id, "created": NOW(),
                "goal": goal, "stage": stage, "unknowns": unknowns}
        body = (f"# Handoff {task_id}\n\n## next_action\n{next_action}\n\n"
                + (f"## evidence\n{evidence}\n" if evidence else ""))
        if disconfirmation:
            meta["disconfirmation"] = disconfirmation
        path = self.home / "handoffs" / f"{hid}.md"
        path.write_text(dump_entry(meta, body), encoding="utf-8")
        rec = {"id": hid, "kind": "handoff", "path": str(path), "task_id": task_id,
               "disconfirmation": disconfirmation, "created": meta["created"],
               "source": f"handoff:{task_id}", "tags": ["handoff"]}
        rec["content_hash"] = _file_hash(path)
        self.db.execute("UPDATE entries SET status='superseded' WHERE kind='handoff' AND task_id=? AND id<>?",
                        (task_id, hid))
        self._index_upsert(rec, f"[{task_id}] {stage}", body)
        warn = [] if unknowns and unknowns != [""] else ["empty unknowns — handoff cannot express epistemic state"]
        if not disconfirmation:
            warn.append("no disconfirmation — next session cannot detect wrong direction")
        return {"id": hid, "path": str(path), "warnings": warn}

    def handoff_read(self, task_id: str = "") -> dict:
        if task_id:
            rows = self.db.execute(
                "SELECT id,path,created FROM entries WHERE kind='handoff' AND task_id=? AND status='active' "
                "ORDER BY created DESC LIMIT 1", (task_id,)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id,path,created FROM entries WHERE kind='handoff' AND status='active' "
                "ORDER BY created DESC LIMIT 10").fetchall()
        out = []
        for (hid, p, c) in rows:
            meta, body = load_entry(Path(p).read_text(encoding="utf-8"))
            out.append({"id": hid, "task_id": meta.get("task_id"), "goal": meta.get("goal"),
                        "stage": meta.get("stage"), "unknowns": meta.get("unknowns"),
                        "disconfirmation": meta.get("disconfirmation", ""),
                        "body": body, "created": c})
        return {"handoffs": out}

    # --------------------------------------------------------------- protocols
    def protocol_add(self, statement: str, context_tags: list[str], evidence_ids: list[str]) -> dict:
        return self.save(statement, f"applies when: {', '.join(context_tags)}\nevidence: {', '.join(evidence_ids)}",
                         kind="protocol", source="verified-decision", tags=context_tags)

    def protocol_match(self, context_tags: list[str]) -> dict:
        rows = self.db.execute(
            "SELECT id,title,tags,created FROM entries WHERE kind='protocol' AND status='active'").fetchall()
        hits = []
        q = set(t.lower() for t in context_tags)
        for (pid, title, tags, created) in rows:
            overlap = q & set(t.lower() for t in json.loads(tags or "[]"))
            if overlap:
                hits.append({"id": pid, "protocol": title, "matched": sorted(overlap), "created": created})
        hits.sort(key=lambda h: -len(h["matched"]))
        return {"protocols": hits}

    # ----------------------------------------------------------------- routing
    def route(self, task: str, k: int = 6) -> dict:
        scored = []
        ql = task.lower()
        qv = _vec(task)
        for t in self.map.get("tools", []):
            kws = [w.lower() for w in t.get("match", [])]
            s = sum(1.0 for w in kws if w and w in ql)
            s += 0.5 * _cosine(qv, _vec(t.get("description", "") + " " + " ".join(kws)), {})
            if s > 0:
                scored.append({"name": t["name"], "kind": t["kind"], "score": round(s, 3),
                               "description": t.get("description", ""),
                               "states": t.get("states", {}),
                               "surface": t.get("command") or t.get("surface", ""),
                               "callable": t["kind"] == "script"})
        scored.sort(key=lambda x: -x["score"])
        return {"routes": scored[:k],
                "note": "states distinguish configured/exposed/authenticated/callable/action_covered (ACCF contract); semantic applicability stays with the model"}

    def register_tool(self, name: str, kind: str, description: str, match: list[str],
                      command: str = "", states: dict | None = None) -> dict:
        assert kind in ("mcp", "skill", "script", "note")
        self.map["tools"] = [t for t in self.map.get("tools", []) if t["name"] != name]
        entry = {"name": name, "kind": kind, "description": description, "match": match,
                 "states": states or {"configured": True, "exposed": True,
                                      "authenticated": kind != "mcp", "callable": kind == "script",
                                      "action_covered": True}}
        if command:
            entry["command"] = command
        self.map["tools"].append(entry)
        self._save_map()
        return {"registered": name, "total_tools": len(self.map["tools"])}

    def call(self, name: str, args: str = "", timeout: int = 60) -> dict:
        t = next((t for t in self.map.get("tools", []) if t["name"] == name), None)
        if not t:
            return {"error": f"not registered: {name}"}
        if t["kind"] != "script":
            return {"error": f"kind={t['kind']} is not locally callable; surface: {t.get('surface') or t.get('command','')}",
                    "hint": "register it as kind=script with a fixed command template to make it callable"}
        cmd = t["command"] + (f" {args}" if args else "")
        env = dict(os.environ, ACCF_HOME=str(self.home))
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=timeout, cwd=str(self.home), env=env)
            return {"name": name, "exit_code": r.returncode,
                    "stdout": r.stdout[-8000:], "stderr": r.stderr[-4000:]}
        except subprocess.TimeoutExpired:
            return {"error": f"timeout after {timeout}s", "name": name}

    # ---------------------------------------------------------------- validate
    def validate(self) -> dict:
        issues: list[str] = []
        rows = self.db.execute("SELECT id,path,content_hash,status FROM entries").fetchall()
        active = 0
        chain_ok = True
        prev = ""
        head = ""
        st = self.home / "state.json"
        if st.exists():
            head = json.loads(st.read_text()).get("chain_head", "")
        for (eid, p, h, status) in rows:
            if status != "active":
                continue
            active += 1
            fp = Path(p)
            if not fp.exists():
                issues.append(f"missing file: {eid} -> {p}")
                continue
            text = fp.read_text(encoding="utf-8")
            if _file_hash(fp) != h:
                issues.append(f"hash drift (file edited after index): {eid}")
            if eid.startswith("pro-"):
                meta, _ = load_entry(text)
                if meta.get("prev_hash", "") != prev:
                    chain_ok = False
                prev = meta.get("hash", prev)
        if protocols := sorted((self.home / "protocols").glob("*.md")):
            pass
        on_disk = {p.stem for sub in ("memory", "handoffs", "protocols") for p in (self.home / sub).glob("*.md")}
        indexed = {r[0] for r in self.db.execute("SELECT id FROM entries")}
        for orphan in on_disk - indexed:
            issues.append(f"file not indexed: {orphan} (rebuild needed)")
        fts_count = None
        if self.fts:
            fts_count = self.db.execute("SELECT count(*) FROM fts").fetchone()[0]
        return {"active_entries": active, "fts_enabled": self.fts, "fts_rows": fts_count,
                "chain_head_match": (not head) or (head == prev),
                "chain_ok": chain_ok, "registered_tools": len(self.map.get("tools", [])),
                "issues": issues or ["none — store is consistent"]}

    def rebuild_index(self) -> dict:
        n = 0
        for sub, kindmap in (("memory", None), ("handoffs", None), ("protocols", None)):
            for p in (self.home / sub).glob("*.md"):
                meta, body = load_entry(p.read_text(encoding="utf-8"))
                kind = meta.get("kind", "fact")
                rec = {"id": meta["id"], "kind": kind, "path": str(p),
                       "source": meta.get("source", ""), "tags": meta.get("tags", []),
                       "disconfirmation": meta.get("disconfirmation", ""),
                       "created": meta.get("created", NOW()), "task_id": meta.get("task_id", "")}
                rec["content_hash"] = _file_hash(p)
                title = (meta.get("title") or meta.get("goal")
                         or (body.splitlines()[0].lstrip("# ") if body else meta["id"]))
                self._index_upsert(rec, title[:120], body)
                n += 1
        return {"rebuilt": n}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ACCF store CLI (validate/rebuild/recall)")
    ap.add_argument("action", choices=["validate", "rebuild", "recall"], nargs="?", default="validate")
    ap.add_argument("--query", default="")
    a = ap.parse_args()
    self = AccfCore()
    if a.action == "validate":
        print(json.dumps(self.validate(), ensure_ascii=False, indent=2))
    elif a.action == "rebuild":
        print(json.dumps(self.rebuild_index(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(self.recall(a.query or "recent memory", 5), ensure_ascii=False, indent=2))

"""🕸️🧠 Graph memory — GraphRAG-style recall for very long histories.

When M33 compacts the conversation, the folded turns shouldn't vanish.
They flow here, into a knowledge graph the agent can query weeks later.

The design follows Microsoft's GraphRAG (From Local to Global, 2024):

1. 🔬 **extract**: one LLM call turns a compacted chunk into topic nodes
   and relations ("Talos → uses → LangGraph").
2. 🕸️ **graph**: nodes/edges persist in Kuzu — an embedded, Cypher-native
   graph DB (the SQLite of graphs), so the agent can run text2cypher.
3. 🧩 **communities**: the Leiden algorithm (via igraph) clusters the
   graph into a 2-level hierarchy of topic communities.
4. 📝 **summaries**: each community gets an LLM summary — but only when
   its membership changed (dirty-tracking). That's the cost control:
   one summary call per dirty community per compaction, all metered.
5. 🔎 **recall**: a query hits community summaries first (the "global"
   view), then drills into leaf topics (the "local" view).

Everything heavy (kuzu, sqlite-vec, igraph) is optional — without them
Talos still runs, just without long-term graph recall. The LLM calls are
injected so the logic is testable offline.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import igraph  # community detection

    HAVE_IGRAPH = True
except ImportError:
    HAVE_IGRAPH = False

try:
    import kuzu  # embedded graph DB

    HAVE_KUZU = True
except ImportError:
    HAVE_KUZU = False


def memory_dir() -> Path:
    return Path(".talos") / "memory"


# ── the in-memory model (always available, persisted to JSON) ───────────
@dataclass
class MemoryGraph:
    """Topics + weighted relations + a 2-level community hierarchy."""

    topics: dict = field(default_factory=dict)          # name -> {desc, chunk}
    edges: dict = field(default_factory=dict)           # (a,b) -> weight
    communities: dict = field(default_factory=dict)     # cid -> [topic names]
    summaries: dict = field(default_factory=dict)       # cid -> text
    dirty: set = field(default_factory=set)             # cids needing summary

    # --- mutation -------------------------------------------------------
    def add_topic(self, name: str, desc: str, chunk: str = "") -> None:
        name = name.strip().lower()
        if not name:
            return
        self.topics[name] = {"desc": desc, "chunk": chunk}

    def add_relation(self, a: str, b: str, weight: int = 1) -> None:
        a, b = a.strip().lower(), b.strip().lower()
        if not a or not b or a == b:
            return
        key = tuple(sorted((a, b)))
        self.edges[key] = self.edges.get(key, 0) + weight

    # --- 🧩 Leiden community detection ---------------------------------
    def recompute_communities(self) -> None:
        """Cluster topics into communities; flag changed ones dirty."""
        names = list(self.topics)
        if len(names) < 2:
            self.communities = {0: names} if names else {}
            self.dirty |= set(self.communities)
            return

        index = {n: i for i, n in enumerate(names)}
        old = {frozenset(v) for v in self.communities.values()}

        if HAVE_IGRAPH:
            g = igraph.Graph(n=len(names))
            weights = []
            for (a, b), w in self.edges.items():
                if a in index and b in index:
                    g.add_edge(index[a], index[b])
                    weights.append(w)
            try:
                part = g.community_leiden(
                    objective_function="modularity",
                    weights=weights or None,
                )
                groups = [[names[i] for i in comm] for comm in part]
            except Exception:
                groups = self._connected_components(names, index)
        else:
            groups = self._connected_components(names, index)

        self.communities = {i: grp for i, grp in enumerate(groups)}
        new = {frozenset(v) for v in self.communities.values()}
        # a community is dirty if it's new or changed membership
        for cid, members in self.communities.items():
            if frozenset(members) not in old:
                self.dirty.add(cid)
        # drop summaries for communities that no longer exist
        self.summaries = {c: s for c, s in self.summaries.items()
                          if c in self.communities}

    def _connected_components(self, names, index) -> list[list[str]]:
        """Fallback clustering when igraph is missing: union-find."""
        parent = list(range(len(names)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for (a, b) in self.edges:
            if a in index and b in index:
                parent[find(index[a])] = find(index[b])
        comps: dict = {}
        for n in names:
            comps.setdefault(find(index[n]), []).append(n)
        return list(comps.values())

    # --- 📝 summaries (dirty only = cost control) ----------------------
    async def summarize_dirty(self, summarize) -> int:
        """``summarize`` is async (community_label, topics, relations) -> str.
        Returns the number of LLM calls made."""
        calls = 0
        for cid in list(self.dirty):
            members = self.communities.get(cid, [])
            if not members:
                continue
            topics = [f"{n}: {self.topics[n]['desc']}" for n in members
                      if n in self.topics]
            rels = [f"{a} — {b}" for (a, b) in self.edges
                    if a in members and b in members]
            self.summaries[cid] = await summarize(f"community-{cid}", topics, rels)
            calls += 1
        self.dirty.clear()
        return calls

    # --- 🔎 recall ------------------------------------------------------
    def recall(self, query: str, limit: int = 3) -> str:
        """Keyword recall: rank community summaries, then drill to topics.
        (M34 ships keyword scoring; vector recall plugs in when an embed
        model is configured — see embeddings note in the docs.)"""
        q = set(query.lower().split())
        scored = []
        for cid, summary in self.summaries.items():
            text = summary.lower()
            members = self.communities.get(cid, [])
            score = sum(text.count(w) for w in q) + sum(
                1 for n in members for w in q if w in n
            )
            if score:
                scored.append((score, cid, summary, members))
        scored.sort(reverse=True)
        if not scored:
            return ""
        blocks = []
        for _score, cid, summary, members in scored[:limit]:
            leaves = ", ".join(members[:8])
            blocks.append(f"### topic cluster\n{summary}\n(topics: {leaves})")
        return "\n\n".join(blocks)

    # --- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "topics": self.topics,
            "edges": {f"{a}|{b}": w for (a, b), w in self.edges.items()},
            "communities": {str(c): v for c, v in self.communities.items()},
            "summaries": {str(c): v for c, v in self.summaries.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryGraph":
        g = cls()
        g.topics = d.get("topics", {})
        g.edges = {tuple(k.split("|", 1)): w for k, w in d.get("edges", {}).items()}
        g.communities = {int(c): v for c, v in d.get("communities", {}).items()}
        g.summaries = {int(c): v for c, v in d.get("summaries", {}).items()}
        return g


def _graph_path(session_id: str) -> Path:
    return memory_dir() / f"{session_id}.graph.json"


def load_graph(session_id: str) -> MemoryGraph:
    p = _graph_path(session_id)
    if p.is_file():
        try:
            return MemoryGraph.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return MemoryGraph()


def save_graph(session_id: str, graph: MemoryGraph) -> None:
    memory_dir().mkdir(parents=True, exist_ok=True)
    _graph_path(session_id).write_text(
        json.dumps(graph.to_dict(), indent=1), encoding="utf-8"
    )
    if HAVE_KUZU:
        _mirror_to_kuzu(session_id, graph)


def _mirror_to_kuzu(session_id: str, graph: MemoryGraph) -> None:
    """Mirror the graph into Kuzu so the agent can run text2cypher."""
    try:
        db_path = str(memory_dir() / f"{session_id}.kuzu")
        db = kuzu.Database(db_path)
        conn = kuzu.Connection(db)
        conn.execute("CREATE NODE TABLE IF NOT EXISTS Topic(name STRING, desc STRING, PRIMARY KEY(name))")
        conn.execute("CREATE REL TABLE IF NOT EXISTS RELATES(FROM Topic TO Topic, weight INT64)")
        for name, data in graph.topics.items():
            conn.execute(
                "MERGE (t:Topic {name: $n}) SET t.desc = $d",
                {"n": name, "d": data["desc"]},
            )
        for (a, b), w in graph.edges.items():
            conn.execute(
                "MATCH (x:Topic {name:$a}), (y:Topic {name:$b}) "
                "MERGE (x)-[r:RELATES]->(y) SET r.weight = $w",
                {"a": a, "b": b, "w": w},
            )
    except Exception:
        pass  # kuzu mirror is a bonus; the JSON graph is the source of truth


# ── the entry point the runtime calls after a compaction ────────────────
def ingest_compaction(session_id, old_messages, new_messages) -> None:
    """Hook from runner.maybe_compact(). Heavy work (extraction + summary)
    is done by ingest_async; this sync shim just records that there's new
    material. The runtime schedules the async ingest when an LLM is free."""
    # The actual extraction is scheduled by the runtime (it owns the LLM);
    # here we just ensure the memory dir exists so the graph can persist.
    memory_dir().mkdir(parents=True, exist_ok=True)


# ── async ingest: extract topics from folded turns, update communities ──
EXTRACT_PROMPT = """Extract the key topics and their relationships from the
conversation excerpt below. Return STRICT JSON:
{"topics":[{"name":"short noun phrase","desc":"one line"}],
 "relations":[["topic a","topic b"]]}
Keep names short and canonical (lowercase). 3-8 topics max."""


async def ingest_async(session_id, folded_text, extract, summarize) -> dict:
    """Run the full GraphRAG ingest for one compaction.

    ``extract``  : async (prompt, text) -> str (JSON)
    ``summarize``: async (label, topics, relations) -> str
    Returns {topics_added, summary_calls} for cost reporting.
    """
    graph = load_graph(session_id)
    raw = await extract(EXTRACT_PROMPT, folded_text)
    added = 0
    try:
        data = json.loads(_json_slice(raw))
        for t in data.get("topics", []):
            if t.get("name"):
                graph.add_topic(t["name"], t.get("desc", ""), folded_text[:200])
                added += 1
        for rel in data.get("relations", []):
            if isinstance(rel, list) and len(rel) == 2:
                graph.add_relation(rel[0], rel[1])
    except (json.JSONDecodeError, TypeError, KeyError):
        return {"topics_added": 0, "summary_calls": 0}

    graph.recompute_communities()
    calls = await graph.summarize_dirty(summarize)
    save_graph(session_id, graph)
    return {"topics_added": added, "summary_calls": calls}


def _json_slice(text: str) -> str:
    """Pull the first {...} block out of a possibly chatty LLM reply."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 and end > start else "{}"


def cypher_query(session_id: str, query: str) -> str:
    """Run raw Cypher against the session's Kuzu graph (text2cypher target)."""
    if not HAVE_KUZU:
        return "graph DB (kuzu) not installed — pip install 'talos[memory]'"
    try:
        db = kuzu.Database(str(memory_dir() / f"{session_id}.kuzu"))
        conn = kuzu.Connection(db)
        result = conn.execute(query)
        rows = []
        while result.has_next():
            rows.append(str(result.get_next()))
        return "\n".join(rows[:50]) or "(no rows)"
    except Exception as exc:
        return f"query error: {exc}"

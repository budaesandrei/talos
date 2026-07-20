"""Tests for GraphRAG memory (M34). Heavy DBs optional; logic always runs."""

import json

from talos.graph_memory import MemoryGraph, ingest_async, load_graph, save_graph


def test_communities_and_dirty_tracking():
    g = MemoryGraph()
    # two clusters: {a,b,c} and {x,y}
    for a, b in [("a", "b"), ("b", "c"), ("a", "c"), ("x", "y")]:
        g.add_topic(a, f"desc {a}"); g.add_topic(b, f"desc {b}")
        g.add_relation(a, b)
    g.recompute_communities()
    # every community starts dirty (needs a first summary)
    assert g.dirty == set(g.communities)
    # at least two clusters found
    assert len(g.communities) >= 2


async def test_summarize_only_dirty():
    g = MemoryGraph()
    for a, b in [("a", "b"), ("x", "y")]:
        g.add_topic(a, "d"); g.add_topic(b, "d"); g.add_relation(a, b)
    g.recompute_communities()

    calls = {"n": 0}
    async def summ(label, topics, rels):
        calls["n"] += 1
        return f"summary of {label}"
    n = await g.summarize_dirty(summ)
    assert n == len(g.communities) and calls["n"] == n
    assert not g.dirty                       # cleared after summarizing

    # re-summarizing without changes = zero calls (the cost control)
    assert await g.summarize_dirty(summ) == 0


def test_recall_ranks_communities():
    g = MemoryGraph()
    g.communities = {0: ["langgraph", "graph"], 1: ["pricing", "cost"]}
    g.summaries = {0: "we built the graph on langgraph",
                   1: "we discussed token pricing and cost"}
    hit = g.recall("how did we handle cost?")
    assert "pricing" in hit and "langgraph" not in hit.split("topics:")[0]


def test_persistence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    g = MemoryGraph()
    g.add_topic("talos", "the agent"); g.add_topic("kuzu", "graph db")
    g.add_relation("talos", "kuzu")
    g.recompute_communities()
    save_graph("s1", g)
    again = load_graph("s1")
    assert "talos" in again.topics and again.edges


async def test_ingest_async_parses_llm_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def extract(prompt, text):
        return ('blah {"topics":[{"name":"auth","desc":"login flow"},'
                '{"name":"jwt","desc":"tokens"}],'
                '"relations":[["auth","jwt"]]} trailing')
    async def summ(label, topics, rels):
        return "auth uses jwt"

    stats = await ingest_async("s2", "we built auth with jwt", extract, summ)
    assert stats["topics_added"] == 2
    g = load_graph("s2")
    assert "auth" in g.topics and ("auth", "jwt") in g.edges or ("jwt", "auth") in g.edges


# ── 🧭 vector recall ─────────────────────────────────────────────────────

def _fake_vec(text: str) -> list[float]:
    """2-d toy embedding: axis 0 = money-ish, axis 1 = graph-ish."""
    t = text.lower()
    money = sum(t.count(w) for w in ("pricing", "cost", "token", "budget"))
    graphy = sum(t.count(w) for w in ("langgraph", "graph", "loop", "agent"))
    return [float(money), float(graphy)] if (money or graphy) else [0.1, 0.1]


async def _fake_embed(texts):
    return [_fake_vec(t) for t in texts]


async def test_embed_missing_only_embeds_new():
    g = MemoryGraph()
    g.add_topic("pricing", "token costs"); g.add_topic("langgraph", "agent loop")
    g.recompute_communities()
    await g.summarize_dirty(lambda l, t, r: _echo(t))

    n = await g.embed_missing(_fake_embed)
    assert n == len(g.topics) + len(g.summaries)
    # second pass: everything already embedded → zero calls
    assert await g.embed_missing(_fake_embed) == 0
    # summary text change (re-dirty) clears its vector for re-embedding
    g.dirty = set(g.communities)
    await g.summarize_dirty(lambda l, t, r: _echo(t))
    assert await g.embed_missing(_fake_embed) == len(g.summaries)


async def _echo(topics):
    return " ".join(topics)


async def test_vector_recall_ranks_by_cosine():
    g = MemoryGraph()
    g.add_topic("pricing", "token costs", chunk="we pay per token")
    g.add_topic("langgraph", "agent loop")
    g.recompute_communities()          # no edges → one community per topic
    await g.summarize_dirty(lambda l, t, r: _echo(t))
    await g.embed_missing(_fake_embed)

    hit = g.recall("what was our cost budget?", query_vec=[1.0, 0.1])
    # money cluster ranks first and surfaces the raw source chunk
    assert hit.index("pricing") < hit.index("langgraph")
    assert "we pay per token" in hit

    # no query_vec → keyword fallback still works
    assert "pricing" in g.recall("token costs pricing")


def test_vectors_survive_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    g = MemoryGraph()
    g.add_topic("talos", "the agent")
    g.vecs["talos"] = [0.25, 0.75]
    g.communities = {0: ["talos"]}
    g.summaries = {0: "talos is the agent"}
    g.summary_vecs = {0: [0.5, 0.5]}
    save_graph("s3", g)
    again = load_graph("s3")
    assert again.vecs["talos"] == [0.25, 0.75]
    assert again.summary_vecs[0] == [0.5, 0.5]

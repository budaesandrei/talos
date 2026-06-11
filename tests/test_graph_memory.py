"""Tests for GraphRAG memory (M34). Heavy DBs optional; logic always runs."""

import json

from talos.memory.graph_memory import MemoryGraph, ingest_async, load_graph, save_graph


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

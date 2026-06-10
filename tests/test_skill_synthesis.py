"""Tests for verified skill synthesis (M40)."""

from talos.skill_synthesis import _parse_candidate, parse_review, synthesize

GOOD = """---
name: deploy-staging
description: ship the current branch to staging safely
---
1. run the test suite: pytest -q
2. build the image and tag it with the git sha
3. push and watch the rollout; roll back if health checks fail
"""


def test_parse_valid_candidate():
    parsed = _parse_candidate(GOOD)
    assert parsed and parsed[0] == "deploy-staging"


def test_parse_rejects_no_skill():
    assert _parse_candidate("NO SKILL") is None


def test_parse_rejects_thin_body():
    assert _parse_candidate("---\nname: x\ndescription: y\n---\ntoo short") is None


async def test_synthesize_saves_only_when_verified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def propose(prompt, _t):
        # propose() is used for both proposal and review here
        if "Review" in prompt:
            return '{"ok": true, "reason": "sound"}'
        return GOOD

    res = await synthesize("did a deploy", propose, propose)
    assert res["saved"] and res["name"] == "deploy-staging"
    assert (tmp_path / ".talos" / "skills" / "deploy-staging" / "SKILL.md").is_file()


async def test_synthesize_blocks_on_failed_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def propose(prompt, _t):
        if "Review" in prompt:
            return '{"ok": false, "reason": "hallucinated commands"}'
        return GOOD

    res = await synthesize("sketchy task", propose, propose)
    assert not res["saved"] and "failed review" in res["reason"]
    assert not (tmp_path / ".talos" / "skills" / "deploy-staging").exists()


def test_parse_review_safe_default():
    assert parse_review("garbage")["ok"] is False

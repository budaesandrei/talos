"""💭 Think-mode stream splitter.

In think mode the model wraps its reasoning in <thinking>…</thinking>
before the answer. Tokens stream in fragments, so a tag can be split
across chunks ("<think" + "ing>"). This incremental splitter feeds on
chunks and yields ("think", text) / ("answer", text) events, holding back
just enough tail to never miss a tag boundary.

The same instance also exposes ``strip()`` to remove thinking blocks from
the final saved message — reasoning is shown live but not persisted.
"""

import re

OPEN, CLOSE = "<thinking>", "</thinking>"
_STRIP_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.S | re.I)


class ThinkSplitter:
    def __init__(self):
        self.in_think = False
        self._carry = ""           # held-back tail that might start a tag

    def feed(self, text: str):
        """Yield (channel, chunk) events. channel ∈ {'think','answer'}."""
        buf = self._carry + text
        self._carry = ""
        # the longest tag prefix we might need to wait for
        keep = max(len(OPEN), len(CLOSE)) - 1

        while buf:
            tag = CLOSE if self.in_think else OPEN
            idx = buf.lower().find(tag)
            if idx == -1:
                # no complete tag; emit all but a possible split-tag tail
                safe = buf[:-keep] if len(buf) > keep else ""
                tail = buf[len(safe):]
                if _maybe_tag_prefix(tail):
                    self._carry = tail
                    buf = safe
                else:
                    safe, self._carry = buf, ""
                    buf = ""
                if safe:
                    yield ("think" if self.in_think else "answer", safe)
                break
            # emit text before the tag, then flip state
            before = buf[:idx]
            if before:
                yield ("think" if self.in_think else "answer", before)
            self.in_think = not self.in_think
            buf = buf[idx + len(tag):]

    def flush(self):
        if self._carry:
            yield ("think" if self.in_think else "answer", self._carry)
            self._carry = ""

    @staticmethod
    def strip(text: str) -> str:
        """Remove <thinking>…</thinking> blocks from a finished message."""
        return _STRIP_RE.sub("", text).strip()


def _maybe_tag_prefix(tail: str) -> bool:
    low = tail.lower()
    return any(t.startswith(low) or low.endswith("<") for t in (OPEN, CLOSE)) \
        or low.endswith("<") or "<thi" in low or "</th" in low

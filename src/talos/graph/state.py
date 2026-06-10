"""🗂️ Graph state — the data that flows through the agent loop.

LangGraph passes a single *state* object between nodes. Each node returns a
partial update, and LangGraph merges it into the state using *reducers*.

``add_messages`` is the key reducer here: instead of replacing the list, it
**appends** new messages (and de-duplicates by message ``id``). That's what
turns a stateless LLM call into a conversation.
"""

from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class AgentState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

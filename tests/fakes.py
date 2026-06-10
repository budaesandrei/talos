"""🎭 A scripted chat model for tests.

Real LLM calls are slow, cost money, and are non-deterministic — terrible
for tests. This fake returns pre-scripted ``AIMessage``s in order, which
lets us test the *graph wiring* (the loop, tool execution, routing)
without any network access.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeToolCallingModel(BaseChatModel):
    responses: list  # consumed front-to-back, one per LLM call

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message: AIMessage = self.responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        # The real model uses the tool schemas; the fake just ignores them.
        return self

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

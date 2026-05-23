from pydantic import BaseModel, Field


class AgentState(BaseModel):
    user_input: str = Field(description="The user's raw input.")
    output: str = Field(default="", description="The agent's final output.")
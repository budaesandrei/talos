# Talos

A LangGraph-based agent runtime with a CLI entry point.

## Setup

```bash
# Install dependencies (uv or pip)
uv sync
# or: pip install -e .

# Configure environment
cp .env.example .env
# Edit .env and set OPENAI_API_KEY
```

## Usage

```bash
# Single message
talos chat "What is the capital of France?"

# Interactive session
talos chat
```

## Project layout

```
src/talos/
  cli.py          CLI entry point
  config.py       Settings from environment
  graph/          LangGraph state and builder
  runtime/        Graph execution
  tools/          Agent tools (extend as needed)
  prompts/        System prompts
```

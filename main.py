"""A LangGraph agent behind FastAPI - the uAgents8 "LangGraph" template.

Kept close to a small, proven shape - one tool node, one model node, pinned dependencies - so
the measured footprint stays small: neither `langchain-openai` nor `langchain-anthropic` is
installed by default (see requirements.txt), both are imported lazily inside respond().

  GET  /      health - the platform only checks that the port accepts connections
  POST /chat  {"message": "..."} -> {"message", "reply", "mode"}

Provider: set LLM_PROVIDER to "openai" (default) or "anthropic", and the matching
OPENAI_API_KEY / ANTHROPIC_API_KEY under Environment Variables with Sensitive on. With no key
set the template still starts and answers - see NO_KEY_MESSAGE below, a deliberately better
first impression than refusing to boot.

Make it yours: replace the body of respond().
"""

import ast
import operator
import os
from typing import TypedDict

import uvicorn
from fastapi import FastAPI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

AGENT_NAME = "template-langgraph"

NO_KEY_MESSAGE = (
    "I can do simple arithmetic without a key. For anything else, add your API key: set "
    "LLM_PROVIDER to \"openai\" or \"anthropic\" and the matching OPENAI_API_KEY or "
    "ANTHROPIC_API_KEY under Environment Variables (Sensitive on), then redeploy."
)


class State(TypedDict):
    message: str
    reply: str
    mode: str  # "tool", "stub" or "model"


# --- calculate: a tool node, no model involved -------------------------------

_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _evaluate(node: ast.AST) -> float:
    """Evaluate + - * / and parentheses on numbers; anything else raises ValueError."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    raise ValueError("unsupported expression")


def _arithmetic(message: str) -> str | None:
    """The expression inside `message`, if the whole message is just arithmetic."""
    expression = message.strip().lower().removeprefix("what is").strip().rstrip("?= ")
    if not expression or not any(ch.isdigit() for ch in expression):
        return None
    if any(ch not in "0123456789+-*/(). " for ch in expression):
        return None
    return expression


def route(state: State) -> str:
    return "calculate" if _arithmetic(state["message"]) else "respond"


def calculate(state: State) -> dict:
    expression = _arithmetic(state["message"]) or ""
    try:
        result = _evaluate(ast.parse(expression, mode="eval"))
    except (SyntaxError, ValueError, ArithmeticError):
        return {"reply": f"I could not calculate {expression!r}.", "mode": "tool"}
    return {"reply": f"{expression} = {result:g}", "mode": "tool"}


# --- respond: where the language model goes ----------------------------------


def _respond_with_openai(message: str) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"reply": NO_KEY_MESSAGE, "mode": "stub"}
    try:
        from langchain_openai import ChatOpenAI  # not in requirements.txt by default
    except ImportError:
        return {"reply": "OPENAI_API_KEY is set but langchain-openai is not in requirements.txt.", "mode": "stub"}
    reply = ChatOpenAI(model=os.environ.get("MODEL_NAME", "gpt-4o-mini")).invoke(message)
    return {"reply": str(reply.content), "mode": "model"}


def _respond_with_anthropic(message: str) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"reply": NO_KEY_MESSAGE, "mode": "stub"}
    try:
        from langchain_anthropic import ChatAnthropic  # not in requirements.txt by default
    except ImportError:
        return {
            "reply": "ANTHROPIC_API_KEY is set but langchain-anthropic is not in requirements.txt.",
            "mode": "stub",
        }
    reply = ChatAnthropic(model=os.environ.get("MODEL_NAME", "claude-3-5-haiku-latest")).invoke(message)
    return {"reply": str(reply.content), "mode": "model"}


def respond(state: State) -> dict:
    """Answer a message that is not arithmetic.

    Reads LLM_PROVIDER ("openai", the default, or "anthropic") and the matching key. With no key
    set this returns NO_KEY_MESSAGE (offline, deterministic) instead of refusing to answer - the
    key is read from the environment, never from this file.
    """
    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
    if provider == "anthropic":
        return _respond_with_anthropic(state["message"])
    return _respond_with_openai(state["message"])


builder = StateGraph(State)
builder.add_node("calculate", calculate)
builder.add_node("respond", respond)
builder.add_conditional_edges(START, route, {"calculate": "calculate", "respond": "respond"})
builder.add_edge("calculate", END)
builder.add_edge("respond", END)
graph = builder.compile()

app = FastAPI(title=AGENT_NAME)


class Chat(BaseModel):
    message: str = Field(min_length=1, max_length=500)


@app.get("/")
def index() -> dict[str, str]:
    """A friendly status route. The platform does not call it: its check is that the port accepts connections."""
    return {"status": "ok", "agent": AGENT_NAME}


@app.post("/chat")
def chat(body: Chat) -> dict[str, str]:
    result = graph.invoke({"message": body.message, "reply": "", "mode": ""})
    return {"message": body.message, "reply": result["reply"], "mode": result["mode"]}


if __name__ == "__main__":
    # The platform sets PORT; listen on all interfaces, not just localhost.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

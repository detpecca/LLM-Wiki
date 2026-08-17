"""Query-time agent: compositional Wiki traversal (paper §3.2, Figure 2).

The agent composes wiki_search / wiki_read calls through a ReAct-style
loop, following links and checking evidence sufficiency, until it answers
or hits a termination condition (paper §3.2 Termination):

  - evidence sufficient (all reasoning chains traced), or
  - tool-call budget T_max = 15 reached, or
  - consecutive empty searches exceed patience P = 3;
  - at least one wiki_read is required before answering.

Tool calls use native function calling when the LLM client supports it
(``chat_tools``), and fall back to prompt-driven JSON actions otherwise —
either statically (no ``chat_tools`` method) or at runtime (the endpoint
rejects the ``tools`` parameter, raising ``ToolsUnsupported``). Both paths
share the same tool-execution logic, termination rules, and trace format.

Strategies (paper Appendix H), chosen adaptively by the agent:
  direct retrieval / link-following traversal / browse & aggregation.
"""

from __future__ import annotations

import json

from . import search
from .llm import ToolsUnsupported
from .store import WikiStore

T_MAX = 15
PATIENCE = 3

_STRATEGY_TEXT = """STRATEGIES:
- Direct retrieval: single-entity question -> one search, read the page.
- Link-following: multi-hop question -> read page A, follow its [[links]]
  to page B, and so on. Each hop uses explicit links, not guesswork.
- Browse & aggregate: open-ended/enumeration question -> read a directory
  index ("<category>/_index") first for an overview, then batch-read the
  promising pages.

RULES:
- After every wiki_read, check sufficiency: do you have ALL the evidence
  the question needs? If not, follow links or issue a REVISED search.
- You must call wiki_read at least once before answering.
- Cite the pages your answer is grounded in."""

# Native function-calling schema (OpenAI tools format).
TOOLS = [
    {"type": "function", "function": {
        "name": "wiki_search",
        "description": "Search the Wiki; returns candidate pages with "
                       "aliases/tags/summaries. Structured signals (entity "
                       "names, aliases) are matched first.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "search terms"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "wiki_read",
        "description": "Batch-read pages or directory indices (e.g. "
                       "'people/_index'). Page content has [[links]] to "
                       "related pages — follow them.",
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "wiki paths like 'dir/Page'"}},
            "required": ["paths"]}}},
    {"type": "function", "function": {
        "name": "answer",
        "description": "Give the final answer once evidence is sufficient. "
                       "Requires at least one prior wiki_read.",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"},
                         "description": "cited page paths"}},
            "required": ["answer"]}}},
]

# System prompt for the JSON-action fallback path: teaches the same three
# tools as a text protocol for endpoints without native function calling.
SYSTEM_PROMPT = """You are a retrieval agent answering questions by traversing a
structured Wiki. You do NOT answer from memory — you gather evidence with
tools first.

TOOLS (reply with exactly one JSON action per turn):
{"tool": "wiki_search", "query": "<search terms>"}
  -> returns candidate pages with aliases/tags/summaries. Structured
     signals are matched first, so entity names and aliases work best.
{"tool": "wiki_read", "paths": ["<dir/Page>", "..."]}
  -> batch-reads pages or directory indices (e.g. "people/_index").
     Page content contains [[links]] to related pages — follow them.
{"tool": "answer", "answer": "<final answer>", "evidence": ["<dir/Page>", ...]}

""" + _STRATEGY_TEXT + """
- Reply with ONE JSON action and nothing else."""

# System prompt for the native tool-calling path: same guidance, minus the
# hand-rolled JSON protocol (the tools are supplied structurally).
SYSTEM_PROMPT_NATIVE = """You are a retrieval agent answering questions by
traversing a structured Wiki. You do NOT answer from memory — you gather
evidence with the provided tools first, then call `answer`.

""" + _STRATEGY_TEXT


def _parse_action(text: str) -> dict | None:
    """Extract the first balanced {...} block (string-aware) as an action.

    Braces inside JSON strings (e.g. an answer containing '}') must not
    affect depth counting, so the scan tracks string/escape state.
    """
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        action = json.loads(text[start:i + 1])
                        if isinstance(action, dict) and "tool" in action:
                            return action
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def _execute(store: WikiStore, action: dict, state: dict, trace: list[dict],
             patience: int) -> dict:
    """Run one tool action and update state. Returns an outcome dict:

    - {"kind": "observation", "text": ...}  -> feed back to the model
    - {"kind": "reprompt", "text": ...}     -> premature answer; ask again
    - {"kind": "answer", "result": {...}}   -> final answer, stop
    - {"kind": "terminate", "result": {...}}-> patience exceeded, stop
    """
    tool = action.get("tool")
    if tool == "wiki_search":
        query = action.get("query", "")
        results = search.search(store, query)
        state["empty"] = state["empty"] + 1 if not results else 0
        trace.append({"tool": tool, "query": query, "hits": len(results)})
        if state["empty"] >= patience:
            trace.append({"tool": "terminate", "reason": "patience exceeded"})
            return {"kind": "terminate", "result": {
                "answer": "(terminated without sufficient evidence)",
                "evidence": [], "trace": trace}}
        obs = json.dumps(results, ensure_ascii=False) if results else "(no results)"
        return {"kind": "observation", "text": obs}
    if tool == "wiki_read":
        paths = [p.removesuffix(".md") for p in action.get("paths", [])]
        contents = store.read_many(paths)
        state["reads"] += 1
        state["empty"] = 0
        trace.append({"tool": tool, "paths": paths})
        obs = "\n\n".join(f"===== {p} =====\n{c}" for p, c in contents.items())
        return {"kind": "observation", "text": obs}
    if tool == "answer":
        if state["reads"] == 0:  # paper: at least one wiki_read before answering
            return {"kind": "reprompt",
                    "text": "You must call wiki_read at least once before answering."}
        trace.append({"tool": "answer", "answer": action.get("answer")})
        return {"kind": "answer", "result": {
            "answer": action.get("answer", ""),
            "evidence": action.get("evidence", []), "trace": trace}}
    return {"kind": "observation", "text": f"(unknown tool '{tool}')"}


def _run_native(store, llm, messages, trace, state, t_max, patience) -> dict | None:
    """Native function-calling loop. Returns a result dict, or None to signal
    a runtime fallback to JSON-action mode (ToolsUnsupported)."""
    messages[0] = {"role": "system", "content": SYSTEM_PROMPT_NATIVE}
    for _step in range(t_max):
        try:
            resp = llm.chat_tools(messages, TOOLS)
        except ToolsUnsupported:
            return None  # caller retries in JSON-action mode
        calls = resp.get("tool_calls") or []
        if not calls:
            messages.append({"role": "assistant", "content": resp.get("content") or ""})
            messages.append({"role": "user",
                             "content": "Use one of the provided tools."})
            continue
        # Protocol: echo the assistant tool_calls, then answer EACH id.
        messages.append({"role": "assistant", "content": resp.get("content"),
                         "tool_calls": [{"id": c["id"], "type": "function",
                             "function": {"name": c["name"],
                                          "arguments": json.dumps(c["arguments"])}}
                             for c in calls]})
        for c in calls:
            action = {"tool": c["name"], **c["arguments"]}
            out = _execute(store, action, state, trace, patience)
            if out["kind"] in ("answer", "terminate"):
                return out["result"]
            text = out["text"]
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": text[:12000]})
    return {"answer": "(terminated without sufficient evidence)",
            "evidence": [], "trace": trace}


def _run_json(store, llm, messages, trace, state, t_max, patience) -> dict:
    """Prompt-driven JSON-action loop (fallback path)."""
    messages[0] = {"role": "system", "content": SYSTEM_PROMPT}
    for _step in range(t_max):
        reply = llm.chat(messages)
        messages.append({"role": "assistant", "content": reply})
        action = _parse_action(reply)
        if action is None:
            messages.append({"role": "user", "content": "Reply with ONE JSON action only."})
            continue
        out = _execute(store, action, state, trace, patience)
        if out["kind"] in ("answer", "terminate"):
            return out["result"]
        if out["kind"] == "reprompt":
            messages.append({"role": "user", "content": out["text"]})
            continue
        messages.append({"role": "user", "content": f"OBSERVATION:\n{out['text'][:12000]}"})
    return {"answer": "(terminated without sufficient evidence)",
            "evidence": [], "trace": trace}


def run_agent(store: WikiStore, llm, question: str,
              t_max: int = T_MAX, patience: int = PATIENCE) -> dict:
    """Run the traversal loop; returns {answer, evidence, trace}.

    Uses native tool calling when ``llm`` exposes ``chat_tools``; otherwise
    (or if the endpoint rejects ``tools`` at runtime) uses JSON actions.
    """
    messages = [
        {"role": "system", "content": ""},  # filled by the chosen driver
        {"role": "user", "content": f"QUESTION: {question}"},
    ]
    trace: list[dict] = []
    state = {"reads": 0, "empty": 0}

    if hasattr(llm, "chat_tools"):
        result = _run_native(store, llm, messages, trace, state, t_max, patience)
        if result is not None:
            return result
        # runtime fallback: reset conversation, keep trace/state, use JSON mode
        messages = [{"role": "system", "content": ""},
                    {"role": "user", "content": f"QUESTION: {question}"}]
    return _run_json(store, llm, messages, trace, state, t_max, patience)

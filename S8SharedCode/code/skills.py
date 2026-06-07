"""Session 8 skill registry + per-skill execution.

The orchestrator (flow.py) treats every node as a `Skill` object loaded
from agent_config.yaml. There is no Python class per skill — that
abstraction would have to be added at the point where a skill needs
behaviour the orchestrator can't infer from the yaml. Today every skill
either calls the gateway or (for sandbox_executor) calls sandbox.py.

What lives here:
  - Skill / SkillRegistry
  - input resolution (`n:...`, `art:...`, `USER_QUERY`, literals)
  - prompt rendering (template + inputs + optional failure report)
  - JSON parsing of the model's reply (single top-level object)
  - the MCP tool schemas exposed to tool-using skills
  - `run_skill(...)` — the dispatcher
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

import artifacts as artifacts_svc
from gateway import LLM
from schemas import AgentResult, NodeSpec

ROOT = Path(__file__).parent
AGENT_CONFIG_PATH = ROOT / "agent_config.yaml"


# ── catalogue ────────────────────────────────────────────────────────────────

class Skill:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.prompt_path = ROOT / cfg["prompt"]
        self.description = cfg.get("description", "")
        self.tools_allowed: list[str] = cfg.get("tools_allowed", []) or []
        self.internal_successors: list[str] = cfg.get("internal_successors", []) or []
        self.critic: bool = bool(cfg.get("critic", False))
        self.provider_pin: str | None = cfg.get("provider_pin")
        # P2 #10: per-skill temperature / max_tokens come from the yaml so
        # tuning a single skill no longer requires a code edit. Defaults
        # are deliberately conservative; a skill that wants exploration
        # (Researcher) bumps temperature; a skill that wants determinism
        # (Critic, Distiller) drops it to ~0.
        self.temperature: float = float(cfg.get("temperature", 0.3))
        self.max_tokens: int = int(cfg.get("max_tokens", 2048))

    def prompt_template(self) -> str:
        if not self.prompt_path.exists():
            return f"You are the {self.name} skill. (Prompt file missing.)"
        return self.prompt_path.read_text()


class SkillRegistry:
    def __init__(self):
        cfg = yaml.safe_load(AGENT_CONFIG_PATH.read_text())
        self._skills: dict[str, Skill] = {n: Skill(n, c) for n, c in cfg.items()}

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"unknown skill: {name}")
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)


# ── input resolution + prompt rendering ──────────────────────────────────────

def resolve_inputs(node_inputs: list[str], graph_nodes, query: str) -> list[dict]:
    """Materialise each input id into a dict the prompt can serialise.

    Recognised input forms:
      - "USER_QUERY"  → the original user query text
      - "n:<i>"       → the AgentResult.output of that completed node
      - "art:<sha>"   → the bytes of an artifact, decoded as utf-8 best-effort
      - any other     → passed through as a free-form string

    `graph_nodes` is the nx node-view dict from flow.Graph; we read each
    upstream node's `result` attribute (set when the orchestrator marks
    the node complete).
    """
    out = []
    for inp in node_inputs:
        if inp == "USER_QUERY":
            out.append({"id": "USER_QUERY", "kind": "query", "value": query})
        elif inp.startswith("n:") and inp in graph_nodes:
            upstream = graph_nodes[inp].get("result")
            if isinstance(upstream, AgentResult):
                out.append({"id": inp, "kind": "upstream",
                            "skill": upstream.agent_name, "output": upstream.output})
            else:
                out.append({"id": inp, "kind": "upstream-missing", "output": None})
        elif inp.startswith("art:"):
            try:
                blob = artifacts_svc.get_bytes(inp)
                text = blob.decode("utf-8", errors="replace")
                out.append({"id": inp, "kind": "artifact", "text": text[:20_000]})
            except Exception as e:
                out.append({"id": inp, "kind": "artifact-missing", "error": str(e)})
        else:
            out.append({"id": inp, "kind": "literal", "value": inp})
    return out


def _format_memory_hits(hits: list) -> str:
    """Compact rendering of FAISS-ranked MemoryItem hits for the prompt.

    Each hit is shown as one line: kind, descriptor, source, plus a 400-char
    preview of `value.chunk` when present (indexed-document chunks) or of
    `value.raw` (classifier facts). The full chunk would blow the prompt,
    but the descriptor + preview is enough for the Planner to decide
    whether memory already covers the query and for downstream skills to
    synthesise from indexed material without an extra Retriever round-trip.
    """
    if not hits:
        return ""
    lines = []
    for h in hits[:8]:  # cap to keep the prompt bounded
        kind = getattr(h, "kind", "?")
        desc = (getattr(h, "descriptor", "") or "")[:200]
        source = getattr(h, "source", "")
        val = getattr(h, "value", {}) or {}
        chunk = val.get("chunk")
        raw = val.get("raw")
        line = f"  - [{kind}] {desc}"
        if source:
            line += f"\n      source: {source}"
        if isinstance(chunk, str) and chunk.strip():
            preview = chunk[:400].replace("\n", " ")
            more = " …" if len(chunk) > 400 else ""
            line += f"\n      chunk: {preview}{more}"
        elif isinstance(raw, str) and raw.strip():
            line += f"\n      raw: {raw[:200]}"
        lines.append(line)
    return "\n".join(lines)


def render_prompt(skill: Skill, query: str, resolved: list[dict],
                  failure_report: str | None = None,
                  memory_hits: list | None = None) -> str:
    parts = [skill.prompt_template().rstrip(), "", f"USER_QUERY: {query}"]
    if failure_report:
        parts += ["", f"FAILURE:\n{failure_report}"]
    # Memory hits — FAISS-ranked MemoryItems from session-start memory.read.
    # Same hits flow into every skill's prompt this run (the S7 contract:
    # every cognitive role can see what the agent already knows).
    hits_block = _format_memory_hits(memory_hits or [])
    if hits_block:
        parts += ["", f"MEMORY HITS ({len(memory_hits)} from FAISS):", hits_block]
    parts += ["", "INPUTS:", json.dumps(resolved, indent=2, default=str)[:20_000]]
    return "\n".join(parts)


def parse_skill_json(text: str) -> dict:
    """Skills return a single top-level JSON object. Strip markdown fences
    if the model added them despite being told not to."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


# ── MCP tool schemas exposed through the gateway tools= channel ──────────────

_TOOL_CATALOG = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    "fetch_url": {
        "name": "fetch_url",
        "description": "Fetch clean markdown from a URL via crawl4ai.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "search_knowledge": {
        "name": "search_knowledge",
        "description": "Vector search over the agent's indexed knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
}


def tool_payload(tool_names: list[str]) -> list[dict] | None:
    if not tool_names:
        return None
    return [_TOOL_CATALOG[n] for n in tool_names if n in _TOOL_CATALOG]


# ── per-node execution ───────────────────────────────────────────────────────

async def run_skill(skill: Skill, node_id: str, graph_nodes,
                    session_id: str, query: str,
                    failure_report: str | None,
                    *, memory_hits: list | None = None) -> tuple[AgentResult, str]:
    """Dispatch one node. Returns (result, rendered_prompt).

    `memory_hits` is the FAISS-ranked MemoryItem list captured once at
    session start by Executor.run and threaded through here so every
    skill's prompt can see the same hits. This is the S7 promise carried
    forward — Memory works in S8 because the orchestrator delivers the
    hits, not just because the FAISS index is on disk.

    sandbox_executor bypasses the gateway: it picks the `code` field out of
    its upstream coder node and runs sandbox.run_python directly. All other
    skills are LLM-backed and route through the V8 gateway with
    agent=<skill_name> so agent_routing.yaml + cost-by-agent kick in."""
    resolved = resolve_inputs(graph_nodes[node_id]["inputs"], graph_nodes, query)
    rendered = render_prompt(skill, query, resolved, failure_report,
                             memory_hits=memory_hits)
    started = time.time()

    if skill.name == "sandbox_executor":
        code = ""
        for r in resolved:
            if r.get("kind") == "upstream" and isinstance(r.get("output"), dict):
                code = r["output"].get("code") or code
        if not code:
            return AgentResult(
                success=False, agent_name=skill.name,
                error="no code in upstream coder output",
                elapsed_s=time.time() - started,
            ), rendered
        from sandbox import run_python
        out = run_python(code)
        return AgentResult(
            success=(out["exit_code"] == 0 and not out["timed_out"]),
            agent_name=skill.name, output=out,
            elapsed_s=time.time() - started,
        ), rendered

    tools = tool_payload(skill.tools_allowed)
    if tools:
        # Multi-turn tool-use loop. mcp_runner opens one MCP stdio session
        # per skill invocation, dispatches each tool_call the model emits,
        # and feeds the results back until the model produces final text.
        from mcp_runner import run_with_tools
        reply = await run_with_tools(
            prompt=rendered,
            tools_payload=tools,
            agent=skill.name,
            session_id=session_id,
            provider_pin=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    else:
        reply = await asyncio.to_thread(
            LLM().chat,
            prompt=rendered,
            agent=skill.name,
            session=session_id,
            provider=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    parsed = parse_skill_json(reply.get("text", ""))

    # Lift orchestrator-recognised fields out of the skill's JSON.
    # NOTES_RUNS feedback P0 #1: malformed successors used to be silently
    # dropped, which left students chasing "missing node" bugs for an hour.
    # Now: log the offending JSON + the validation error, then fail the
    # node so the failure path (and replay) surfaces it.
    raw_successors = parsed.pop("successors", []) or []
    successors: list[NodeSpec] = []
    rejected: list[str] = []
    for s in raw_successors:
        try:
            successors.append(NodeSpec.model_validate(s))
        except ValidationError as ve:
            rejected.append(f"successor={s!r}  error={ve}")
    if skill.name == "planner":
        for s in parsed.get("nodes", []) or []:
            try:
                successors.append(NodeSpec.model_validate(s))
            except ValidationError as ve:
                rejected.append(f"node={s!r}  error={ve}")

    if rejected:
        err = (
            f"{skill.name}: {len(rejected)} malformed NodeSpec(s) emitted.\n"
            + "\n".join(f"  - {line}" for line in rejected)
        )
        print(f"[skills] {err}")
        return AgentResult(
            success=False, agent_name=skill.name,
            output=parsed, successors=successors,
            elapsed_s=time.time() - started,
            provider=reply.get("provider", ""),
            error=err,
        ), rendered

    return AgentResult(
        success=True,
        agent_name=skill.name,
        output=parsed,
        successors=successors,
        elapsed_s=time.time() - started,
        provider=reply.get("provider", ""),
    ), rendered

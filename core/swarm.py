# -*- coding: utf-8 -*-
"""
Swarm-Koordinator für glyph-agent — schlanke Multi-Agent- und Delegations-Architektur.

Ermöglicht:
- Definition spezialisierter Agenten/Rollen (z. B. Recherche, Synthese, Code-Prüfung)
- Handoff / Weitergabe von Aufgaben zwischen Agenten
- Parallele Teilaufgaben (Fan-Out / Gather)
- Pipeline-Ausführung (Sequenz von Spezialisten)

Folgt den Glyph-Prinzipien: stdlib-basiert, kein schweres Framework, nutzt core.llm und core.log.
"""
from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from . import llm, log


@dataclass
class SwarmAgent:
    """Repräsentiert einen spezialisierten Agenten innerhalb eines Swarms."""
    name: str
    role: str
    system_prompt: str
    model: Optional[str] = None
    tools: Dict[str, Callable[..., Any]] = field(default_factory=dict)
    max_turns: int = 5

    def get_instructions(self, extra_context: str = "") -> str:
        base = self.system_prompt.strip()
        if extra_context:
            return f"{base}\n\nZusätzlicher Kontext:\n{extra_context}"
        return base


@dataclass
class SwarmResponse:
    """Ergebnis einer Swarm- oder Agenten-Ausführung."""
    agent_name: str
    content: str
    history: List[Dict[str, Any]] = field(default_factory=list)
    handoff_to: Optional[str] = None
    tool_calls_count: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)


class Swarm:
    """
    Orchestriert die Ausführung und Interaktion mehrerer Agenten.
    """

    def __init__(self, default_agent: Optional[SwarmAgent] = None):
        self.agents: Dict[str, SwarmAgent] = {}
        if default_agent:
            self.register(default_agent)
        self.default_agent_name = default_agent.name if default_agent else None

    def register(self, agent: SwarmAgent) -> Swarm:
        """Registriert einen neuen Agenten im Swarm."""
        self.agents[agent.name] = agent
        if not self.default_agent_name:
            self.default_agent_name = agent.name
        return self

    def get_agent(self, name: Optional[str] = None) -> SwarmAgent:
        """Liefert den Agenten anhand des Namens oder den Standard-Agenten."""
        target = name or self.default_agent_name
        if not target or target not in self.agents:
            raise ValueError(f"Agent '{target}' nicht im Swarm registriert. Verfügbar: {list(self.agents.keys())}")
        return self.agents[target]

    def run(
        self,
        task: str,
        start_agent: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        max_handoffs: int = 4,
    ) -> SwarmResponse:
        """
        Führt eine Aufgabe iterativ aus, inklusive automatischer Handoff-Weiterleitung.
        """
        current_agent = self.get_agent(start_agent)
        history: List[Dict[str, Any]] = []
        ctx = dict(context or {})
        handoff_count = 0
        current_task = task

        log.log("swarm_start", agent=current_agent.name, task_preview=task[:120])

        while handoff_count <= max_handoffs:
            agent_prompt = current_agent.get_instructions(
                f"Bisheriger Verlauf / Kontext:\n{json.dumps(ctx, ensure_ascii=False, indent=2)}" if ctx else ""
            )

            # Handover-Optionen in den Systemprompt einspeisen
            available_agents = [a for a in self.agents if a != current_agent.name]
            if available_agents:
                handoff_hint = (
                    f"\n\nHandoff-Regel: Falls die Aufgabe besser von einem anderen Spezialisten erledigt werden soll, "
                    f"antworte im Format:\nHANDOFF: <AgentenName>\nGRUND: <Grund/Aufgabe für den nächsten Agenten>\n"
                    f"Verfügbare Spezialisten: {', '.join(available_agents)}"
                )
                agent_prompt += handoff_hint

            provider = llm.get_provider()
            reply = provider.chat(agent_prompt, current_task)
            
            entry = {
                "agent": current_agent.name,
                "task": current_task,
                "response": reply,
                "timestamp": time.time(),
            }
            history.append(entry)

            # Prüfe auf Handoff-Trigger
            handoff_target = None
            if "HANDOFF:" in reply:
                lines = reply.splitlines()
                for line in lines:
                    if line.strip().startswith("HANDOFF:"):
                        target_candidate = line.replace("HANDOFF:", "").strip()
                        if target_candidate in self.agents:
                            handoff_target = target_candidate
                            break

            if handoff_target and handoff_target != current_agent.name:
                handoff_count += 1
                log.log("swarm_handoff", from_agent=current_agent.name, to_agent=handoff_target)
                ctx[f"{current_agent.name}_output"] = reply
                current_task = f"Übergabe von {current_agent.name}. Bitte fortführen mit folgender Zwischenausgabe:\n{reply}"
                current_agent = self.agents[handoff_target]
                continue

            # Keine weitere Übergabe -> Fertig
            log.log("swarm_complete", final_agent=current_agent.name, handoffs=handoff_count)
            return SwarmResponse(
                agent_name=current_agent.name,
                content=reply,
                history=history,
                handoff_to=None,
                meta={"handoff_count": handoff_count, "context": ctx},
            )

        return SwarmResponse(
            agent_name=current_agent.name,
            content=reply,
            history=history,
            handoff_to=None,
            meta={"handoff_count": handoff_count, "max_handoffs_reached": True, "context": ctx},
        )

    def pipeline(self, task: str, agent_names: List[str]) -> SwarmResponse:
        """
        Führt eine Liste von Agenten sequentiell als Pipeline aus.
        Der Output jedes Agenten wird der Input des nächsten.
        """
        if not agent_names:
            raise ValueError("agent_names darf nicht leer sein.")

        current_input = task
        history: List[Dict[str, Any]] = []

        for name in agent_names:
            agent = self.get_agent(name)
            log.log("swarm_pipeline_step", agent=agent.name)
            provider = llm.get_provider()
            output = provider.chat(agent.get_instructions(), current_input)
            history.append({"agent": agent.name, "input": current_input, "output": output})
            current_input = output

        return SwarmResponse(
            agent_name=agent_names[-1],
            content=current_input,
            history=history,
            meta={"pipeline_agents": agent_names},
        )

    def fan_out(
        self, subtasks: Dict[str, str], max_workers: int = 4
    ) -> Dict[str, SwarmResponse]:
        """
        Führt mehrere Teilaufgaben parallel mit den jeweils zugewiesenen Agenten aus.
        Format: { agent_name: task_prompt }
        """
        results: Dict[str, SwarmResponse] = {}

        def _run_single(agent_name: str, prompt: str) -> tuple[str, SwarmResponse]:
            agent = self.get_agent(agent_name)
            provider = llm.get_provider()
            reply = provider.chat(agent.get_instructions(), prompt)
            resp = SwarmResponse(
                agent_name=agent.name,
                content=reply,
                history=[{"agent": agent.name, "task": prompt, "response": reply}],
            )
            return agent_name, resp

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_run_single, name, p) for name, p in subtasks.items()
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    name, resp = fut.result()
                    results[name] = resp
                except Exception as e:
                    log.log("swarm_fanout_error", error=str(e))

        return results


def _parse_queries(raw, topic, limit=3):
    """JSON-Queries aus dem Planer; Fallback = das Thema selbst."""
    text = str(raw or "").strip()
    queries = []
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(text[start : end + 1])
            qs = obj.get("queries") if isinstance(obj, dict) else None
            if isinstance(qs, list):
                queries = [str(q).strip() for q in qs if str(q).strip()]
    except Exception:
        queries = []
    if not queries:
        queries = [str(topic or "").strip()]
    out = []
    seen = set()
    for q in queries:
        q = q[:200]
        key = q.lower()
        if not q or key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out or [str(topic or "").strip()]


def run_swarm(
    topic,
    *,
    chat_fn=None,
    search_fn=None,
    max_queries=3,
    on_event=None,
):
    """
    Composer-Swarm: Planer → Websuche → Synthese (B+-Bericht mit URLs).
    chat_fn/search_fn nur für Tests; Default = core.llm.chat / web.web_search.
    """
    from . import llm, web

    topic = str(topic or "").strip()
    if not topic:
        return {
            "ok": False,
            "answer": "Swarm braucht ein Thema.",
            "error": "Swarm braucht ein Thema",
            "rounds": 0,
            "tool_calls": [],
            "swarm": True,
        }

    chat = chat_fn or llm.chat

    def _search(query, count=4):
        if search_fn:
            return search_fn(query, count)
        # Swarm verdichtet Treffer auf Snippets — kein Volltext-Fetch (attach_top=0).
        return web.web_search(query, count=count, source="both", attach_top=0)

    def emit(event):
        if not on_event:
            return
        try:
            on_event(event)
        except Exception:
            pass

    emit({"type": "step", "action": "SwarmPlan", "status": "start", "detail": topic[:120]})
    plan_sys = (
        "Zerlege das Thema in 2–3 öffentliche Web-Suchanfragen. "
        "Keine privaten Vault- oder Personen-Daten. "
        'Nur JSON: {"queries": ["..."]}.'
    )
    raw_plan = chat(plan_sys, topic)
    queries = _parse_queries(raw_plan, topic, limit=max_queries)
    emit({
        "type": "step",
        "action": "SwarmPlan",
        "status": "done",
        "detail": ", ".join(queries),
    })

    hits = []
    emit({
        "type": "step",
        "action": "SwarmSearch",
        "status": "start",
        "detail": str(len(queries)),
    })
    for q in queries:
        try:
            rows = _search(q, 4) or []
        except Exception as e:
            log.log("swarm_search_error", query=q, error=str(e))
            rows = []
        if not isinstance(rows, list):
            rows = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            if not url:
                continue
            hits.append({
                "query": q,
                "title": str(r.get("title") or "")[:200],
                "url": url[:500],
                "snippet": str(r.get("snippet") or "")[:400],
            })
    emit({
        "type": "step",
        "action": "SwarmSearch",
        "status": "done",
        "detail": f"{len(hits)} Quellen",
    })

    emit({"type": "step", "action": "SwarmSynthese", "status": "start"})
    sources_block = "\n".join(
        f"- {h['title']} — {h['url']}\n  {h['snippet']}" for h in hits[:12]
    ) or "(keine Treffer)"
    syn_sys = (
        "Du bist glyph-agent. Knappen B+-Bericht schreiben. Kern zuerst. "
        "Jede faktische Aussage mit URL aus den Quellen. "
        "Quellen NOTIEREN: URL direkt hinter der Aussage, höchstens eine je Aussage. "
        "Ohne Klammern, ohne Semikolon, ohne Wort 'WEB' vor der URL. "
        "Keine erfundenen Quellen. Unsicherheit sagen. stop-slop. Deutsch."
    )
    answer = chat(syn_sys, f"Thema: {topic}\n\nQuellen:\n{sources_block}")
    emit({"type": "step", "action": "SwarmSynthese", "status": "done"})
    if answer:
        emit({"type": "answer", "status": "content", "text": answer})

    return {
        "ok": True,
        "answer": answer or "",
        "rounds": 2,
        "tool_calls": [
            {"tool": "WebSearch", "args": {"query": q}} for q in queries
        ],
        "queries": queries,
        "sources": hits[:12],
        "swarm": True,
    }

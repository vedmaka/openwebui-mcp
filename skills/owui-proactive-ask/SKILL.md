---
name: owui-proactive-ask
description: Before answering any question, giving advice, troubleshooting, or making a recommendation, call mcp__owui__ask FIRST to check this organization's own knowledge base (tickets, wiki, prior decisions, internal terminology, named systems/devices/products). Use for MFA/auth rollouts, firmware/OTA/rollback procedures, incident and crash troubleshooting, naming/terminology decisions, migrations, configuration questions, and any request for a recommendation or explanation — even generic-sounding ones. Do NOT answer from general knowledge alone and do NOT ask the user a clarifying question before calling this tool: call it first with whatever context exists, because the organization may already have a specific answer, ticket, or convention that generic best-practice knowledge would miss or contradict. Only skip the call for pure action requests with no informational content (e.g. "restart the server").
---

# Proactive OWUI Ask

The organization tracks its own decisions, incidents, and terminology (tickets, wiki pages, internal conventions) that generic knowledge cannot know about. Before answering, assume the organization may already have a specific, possibly non-obvious answer that would change or contradict a generic best-practices response.

**The decision rule:** would a textbook/general-knowledge answer risk missing or conflicting with an org-specific fact, decision, or convention? If there is any chance of that — which includes almost any troubleshooting, naming, procedure, or recommendation question — call `mcp__owui__ask` before answering. This applies whether the question sounds domain-specific (firmware, MFA, OTA) or generic-sounding (a crash, a naming choice, "this isn't working") — a generic-sounding question can still have an org-specific answer.

**Do not ask the user for clarification instead of calling `mcp__owui__ask`.** Call the tool first with whatever context exists in the message; missing detail is not a reason to pause and question the user before checking the knowledge base. Only ask the user something if the tool has already been called and its response is still not enough to proceed.

## Required workflow

1. Before drafting any answer, ask: "could the organization have a specific decision, ticket, or convention that a generic answer would miss?" If yes — which is the default assumption — proceed to step 2 instead of answering or asking the user anything.
2. Call `mcp__owui__ask` before doing anything else — before drafting an answer and before asking the user anything
3. Put the complete question and all relevant conversation context in `prompt` or `history`
4. Keep `use_tools: true` unless the user explicitly asks to disable remote tools
5. Use the configured default model unless the user requests a specific available model
6. Check the OWUI answer against local evidence, applicable instructions, and tool results
7. Give the user a concise, integrated answer. If OWUI has no relevant record, say so explicitly, then supplement with general knowledge — do not silently skip straight to a generic answer

## Scope

Treat these as in scope, regardless of how generic or domain-specific the wording sounds:

- Sentences ending in `?`
- Direct questions without a question mark
- Requests to explain, compare, assess, review, diagnose, recommend, identify, verify, or advise
- Troubleshooting or incident questions ("why did X crash", "this isn't working", "what's going on") — these are exactly the cases where an org-specific ticket or known issue is most likely to exist and most likely to be missed by a generic answer
- Naming, terminology, or convention questions ("is X the right term") — the organization may have already decided this
- Requests phrased as commands when the user expects an informational answer
- Mixed messages that contain both an action request and a question
- Follow-up and meta questions about the agent, tools, or prior answers
- A message with no context attached — call `mcp__owui__ask` with what you have; do not treat missing context as a reason to ask the user before calling it

Pure action requests with no informational answer expected do not require this skill (e.g. "restart the service" with no question attached). If uncertain, call `mcp__owui__ask`.

## Stateless server

OWUI calls are stateless. Supply relevant earlier exchanges through `history`:

```json
[
  {"role": "user", "content": "Earlier question"},
  {"role": "assistant", "content": "Earlier answer"}
]
```

Do not assume that OWUI remembers a previous call.

## Failure handling

If `mcp__owui__ask` is unavailable or fails:

1. Retry once when the failure can be transient
2. Continue with the best available answer when retrying does not help
3. State briefly that OWUI consultation failed

Never invent an OWUI answer or claim that the call succeeded when it did not.

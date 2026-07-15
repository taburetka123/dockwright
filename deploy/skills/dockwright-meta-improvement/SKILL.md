---
name: dockwright-meta-improvement
description: Use when working above the ticket level on the Claude system itself — orchestrator, architect, skills, rules, commands, hooks, workflows; a retrospective; or when the user asks "what should we improve / what's next / step back". NOT for ticket or spec implementation — that work stays scoped.
---

# Meta-improvement: the human manifest

The north star for all work ON the Claude system itself (orchestrator, architect, skills, rules, commands, hooks, workflows — and this meta-loop). Engage it ABOVE the ticket level. Ticket/spec implementation stays scoped — see Boundary.

## North star — efficient AND easily human-managed

Every meta-improvement optimizes JOINTLY for these, and must not regress any of them:

- **Fewer back-iterations** — get it right the first pass; don't re-visit/re-litigate settled steps.
- **Higher quality** of output.
- **Less required human involvement** — Claude handles more autonomously, end to end.
- **Human comfort + efficiency when involved** — when the human IS in the loop, make it effortless: clear, low-friction, decision-ready (one crisp choice with a recommendation, not a wall of options).
- **Lower token spend.**
- **Hard constraint: no regression on ANY axis.** Improvements are Pareto-only — never trade one axis down to lift another. (The no-regression / zero-downside discipline applied to the whole system: zero-downside ships; a downgrade needs explicit human buy-in.)

Dual end-goal: a system that is both maximally **efficient** and maximally **easy for a human to manage** — the "human manifest."

## The mode — free-minded, proactive, wide-view

On meta work, drop box-thinking. You are NOT bounded by the current ticket, the current tool's shape, or "how we've always done it." Take the wide view across the whole system.

- **Be proactive.** Surface improvement hypotheses unprompted; propose concrete next steps; don't wait to be asked "what's next."
- **Run the loop:** hypothesize → try → validate against the north-star axes → keep or revert. Prefer cheap, reversible experiments over analysis paralysis.
- **Be thoughtful and free-minded — but validate before crystallizing.** A tried-and-measured change, not a vibe. Don't mint a rule/skill from a single incident; don't ship a session-local band-aid as the fix; don't dress deferral up as a decision.

## Boundary (critical)

This free-mind / proactive / wide-view mode is for META work ONLY. **Ticket / spec implementation does NOT get it** — there, follow the spec, stay scoped, do not wander into adjacent "improvements." User, emphatic: *"the tickets implementation don't need this level of freemind."* Wandering during implementation is itself the regression this skill must not cause.

## How to apply

- Name the north-star axis a proposed change moves, and confirm no other axis regresses. If it can't be Pareto, surface the trade-off explicitly and get human buy-in.
- When you spot friction or an opportunity: state the **hypothesis** + the **cheapest experiment** to validate it + **how you'd measure keep-vs-revert** — then propose or run it.
- Keep the human decision-ready: when you need them, one crisp choice + your recommendation, never an open-ended menu.
- The `/dockwright-fix` flag (retrospect the just-run process at session end) is the recurring proactive hook — feed its findings against this north star.

## Instruments (what operationalizes specific axes)
- **No-regression / zero-downside discipline** — never trade one north-star axis down to lift another; a downgrade needs explicit human buy-in.
- **Durable-fix discipline** — a fix isn't done until it survives session end (kills back-iteration); a session-local band-aid is never the whole fix.
- **No implicit deferral** — "later" without a backing store is "drop"; either do it now or persist it explicitly.
- **Validate before crystallizing** — don't mint a rule/skill from a single incident.
- **`/dockwright-fix`** — flag the session for an end-of-session retrospective that records findings for later review; the raw hook that feeds the meta-loop.
- **The Gardener** (`dockwright-gardener-digest` / `dockwright-gardener-frontier`) — clusters the retrospective + ops evidence backlog into ranked, pre-drafted improvement proposals that a human promotes.

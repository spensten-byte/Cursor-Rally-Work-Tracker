Render instructions (used by template engine, not LLM):

- One-screen executive summary with a KPI row, per-person work cards, and supporting detail sections.
- Sections rendered by the Jinja template directly from the extract JSON (no LLM re-processing):
  - KPI row: counts of active projects, blocked projects, watch items, and completed items (computed from `team_members`).
  - Capacity callout: shown automatically when any `team_members` entry has `workload == "at_capacity"` or `"heavy"`.
  - Team work cards: one card per `team_members` entry, listing each project with LOE, status, target, and dependencies tags.
  - `at_a_glance`, `decisions`, `closures`, `new_tracks`, `reassignments`, `watch_list`, `gaps`, `next_steps`: rendered as bullet lists.
- Omit empty sections from the rendered output.
- Use concise bullet phrasing matching team standup style.
- Preserve owner names and dates exactly as extracted.

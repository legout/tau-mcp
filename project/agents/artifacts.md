# Artifact mapping

Mapping: generated defaults. This file is declarative documentation, not executable configuration.

- project/research/: investigations, design studies, and probe reports.
- project/adr/: accepted architectural decisions.
- project/specs/: behavioral contracts.
- project/plans/: execution maps.
- project/tickets/: local work items.
- project/agents/: workflow configuration, including the tracker (project/agents/issue-tracker.md) and the context layout (project/agents/domain.md).
- CONTEXT.md: canonical domain vocabulary per the context layout declared in project/agents/domain.md.

Explicit project mappings recorded here override these defaults. Planning artifact and handoff semantics are owned by the `planning-contract` skill from `legout/skills`.

Setup never moves existing documents and never fabricates glossaries, ADRs, or placeholder folders to match this map. A misplaced document is evidence of misclassification, not a mapping rule; resolve conflicts explicitly with the owner.

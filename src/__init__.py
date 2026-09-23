"""Agent harness layer (v5).

This package is a *sidecar* to the existing pipeline, not a replacement:
`agents/`, `orchestrator/`, `tools/` keep working exactly as before when no
harness is active. Everything here depends one-way on the legacy modules;
the only place a legacy module knows about `src/` is the single hook in
`tools/tool_gateway.py`.
"""

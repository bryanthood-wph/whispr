"""whispr pipeline: prepare -> extract -> write -> maintain (docs/plan/D-architecture-and-ops.md).

Separate from `whispr/` (the recorder), which stays local-only. Every model call
goes through pipeline/models.py; every tunable comes from config/ via pipeline/config.py.
"""

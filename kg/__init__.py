"""whispr knowledge graph + pipeline state store (docs/plan/C-knowledge-graph.md C.4).

kg/db.py opens and migrates the database; kg/store.py is the one data-access module
for the graph and tasks; kg/state.py holds runs, items and alerts. Code writes the
graph, never a model.
"""

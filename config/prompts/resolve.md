<!-- "Same entity?" decision for kg/resolve.py (C.5 step 1, kg.er.prompt). Code fills the placeholders: the entity type, why the pair was flagged, and each record as JSON (name, aliases, emails, a few facts and edges with their transcript quotes). Output schema: config/kg/resolve.json (kg.er.schema). -->
YOUR TASK
A personal knowledge graph is built from the owner's meeting transcripts. It holds
two {{ENTITY_TYPE}} records that might be one real {{ENTITY_TYPE}}, recorded twice.
Decide whether they are the same. Return only the JSON the schema asks for.

WHY THEY WERE PAIRED
{{SIGNALS}}

RECORD A
{{ENTITY_A}}

RECORD B
{{ENTITY_B}}

HOW TO DECIDE
- "same": the records are one {{ENTITY_TYPE}}. Typical evidence: one name spelled
  two ways (speech recognition misspells names: "Jon" / "John", "Priya Shaw" /
  "Priya Shah"), the same email, or the same projects, teams and work in both.
- "different": the evidence shows two. Typical evidence: different email addresses
  with different work, or facts and edges that cannot both be about one
  {{ENTITY_TYPE}}.
- "unsure": the evidence does not settle it.
- A shared first name proves nothing: many different people share one.
- One person can have two email addresses, so different emails alone are not proof.
- Prefer "unsure" to a guess. A wrong merge mixes two records' facts and tasks; an
  unsure pair is only reviewed later.
- `reason`: one sentence naming the evidence you relied on.

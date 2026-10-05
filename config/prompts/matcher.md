<!-- Order: call context and transcript first, byte-identical in every prompt on one transcript (so prompt caching applies); the task comes last. -->
CALL CONTEXT (from the recording's metadata)
- Title: {{CALL_TITLE}}
- Date: {{DATE}}
- Type: {{CALL_TYPE}}
- Organizer: {{ORGANIZER}}
- Attendees: {{ATTENDEES}}
- Recording owner: {{OWNER_NAME}}, shown in the transcript as "Me"

TRANSCRIPT
{{TRANSCRIPT}}
END OF TRANSCRIPT

YOUR TASK
Two people independently listed the items worth keeping from the call above.
Pair each item in LIST A with the item in LIST B that is the same item, if
there is one. Return only the JSON the schema asks for.

HOW TO READ AN ITEM
Each item is one JSON object: `id`, `type` (decision, task, number, risk,
open_question or fact), `text` (the item in the lister's own words),
`owner` / `owner_basis` / `mine` (tasks only; "Me" and `mine` true mean the
recording owner, {{OWNER_NAME}}, owns the task), `due` (a task's deadline as
stated) and `quote` (the transcript words the lister cited).

WHAT "THE SAME ITEM" MEANS
Two items are the same when they record the same thing said on the call:
someone holding one of them has the other.
- Pair them even when the wording, length or detail differs.
- Pair them even when they quote different words for the same thing, such as
  one quoting a request and the other quoting the reply that accepts it. Use
  the transcript to check.
- Pair items that report the same statement even if their fields disagree:
  a different `type`, `owner`, `owner_basis`, `mine` or `due` does not make
  them different items. Judge what was said, not how it was labelled.
- Do NOT pair different statements on the same subject: two separate tasks
  from one sentence, a question and its later answer, two different figures
  about the same topic, a risk and the decision taken because of it.
Each item pairs with at most one item from the other list. Leave an item
unpaired when nothing in the other list is the same item; unpaired items are
normal. Copy every id exactly as given.

LIST A
{{ITEMS_A}}

LIST B
{{ITEMS_B}}

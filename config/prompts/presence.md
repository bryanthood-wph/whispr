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
Below is one item that someone extracted from the call above. Check it
against the transcript: is it really in the call, as written? It may be right
or wrong; judge only from the transcript. Answer "present" or "absent" and
return only the JSON the schema asks for.

WHO SAID WHAT
- The recording owner is "Me". Every "Me:" line is {{OWNER_NAME}}.
- "Others:" lines are one combined far-end channel; it may be several people.
- The transcript may contain mis-hearings. Read charitably from context, but
  never accept facts that are not there.

HOW TO READ THE ITEM
- `type`: decision (something decided or agreed), task (work someone is to do
  after the call, including a request made of someone), number (a figure,
  date, amount or metric), risk (a risk, blocker or concern), open_question
  (a question still unanswered when the call ends) or fact (any other stated
  fact).
- `text`: the item in the extractor's own words.
- `owner`, `owner_basis`, `mine` describe tasks only. `owner` "Me" means the
  recording owner. `owner_basis` is "assigned" (the call gives the task to
  Me), "volunteered" (Me offers or commits to it), "others" (someone other
  than Me owns it) or "unclear" (the transcript can't settle who owns it).
  `mine` is true only when Me owns the task. For other types these fields are
  null / "unclear" / false and say nothing.
- `due`: a task's deadline as stated, or null when none was stated.
- `quote`: the transcript words the extractor cited. Find them and read them
  in the context of the lines around them.

ANSWER "present" ONLY IF ALL OF THESE HOLD
1. The call contains what the item says, read in context: not a misreading of
   the quote, a hypothetical taken as a commitment, a question taken as a
   decision, a joke, or something reversed later on the call.
2. The `type` fits what was said.
3. For a task: the owner is right, and `mine` is true exactly when Me owns
   it. A due date is given only if one was stated, and matches it.
4. Nothing in the item is invented: no added detail the call does not state.
Otherwise answer "absent". The wording need not match the transcript; judge
the substance.

THE ITEM
{{ITEM}}

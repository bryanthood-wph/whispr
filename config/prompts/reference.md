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
You build the reference record for the machine-transcribed Microsoft Teams
call above: every decision, task, number, risk, open question and fact worth
keeping, each tied to the exact transcript words that support it. Call
summaries are scored against this record, so a missed item and an invented
item are both scoring errors. Completeness and accuracy matter more than
brevity. Return only the JSON the schema asks for.

WHO SAID WHAT
- The recording owner is "Me". Every "Me:" line is {{OWNER_NAME}}, speaking
  into their own microphone.
- "Others:" lines are one combined far-end channel with no per-person labels.
  It may be several different people.
- When someone addresses {{OWNER_NAME}} by name, or by a short form of the
  name, they are addressing Me.
- A name inside what someone says is a mention, not the speaker.
- The transcript may contain mis-hearings. Read charitably from context, but
  never add facts that are not there.

ITEM TYPES (`type`): give each item the one type that fits best
- decision: something the call decided or agreed ("we'll go with option B",
  "the deck ships Friday").
- task: a piece of work someone is to do after the call: assigned to someone,
  volunteered, or requested of someone. A request counts even if no one
  answers it. Not a task: work already finished, an idea nobody takes on
  ("we could look at…" with no agreement to do it), or an action completed
  during the call itself (sharing a screen, opening a file).
- number: a figure, date, amount or metric stated on the call (revenue, a
  headcount, a launch date, a percentage).
- risk: a stated risk, blocker or concern.
- open_question: a question raised on the call and still unanswered when the
  call ends. A question answered later on the call is not open; record the
  answer instead if it is worth keeping.
- fact: any other stated fact worth keeping.
One statement is one item. Do not record the same statement twice under two
types; choose the type a reader would look under. A deadline belongs in its
task's `due`, not in a separate number item. Two separate pieces of work in
one sentence ("send the data request and the access request") are two task
items, which may share a quote.
List each task exactly once, even when it is requested in one turn and
accepted in another ("Can you send the deck?" … "Sure, by Friday"). Quote the
commitment turn, where the owner takes it on; quote the request only when no
one takes it on.

FIELDS
- `text`: a short, self-contained statement of the item in plain words. Say
  what "it" or "that" refers to.
- `quote`: words copied verbatim from the transcript that show the item.
  - Copy one continuous span from a single transcript line, character for
    character: same spelling, same punctuation, same mis-hearings.
  - Leave out the timestamp and the "Me:" / "Others:" label.
  - No ellipses, no joined fragments, no corrections, no added words.
  - Choose the shortest span that on its own shows the item, usually one
    clause or sentence.
  - If you can't quote it, leave the item out. A program checks every quote
    against the transcript and discards any item whose quote isn't there.
- `owner`, `owner_basis`, `mine`: these describe tasks only.
  - For a task:
    - `owner`: "Me" when the recording owner owns it. Otherwise the person's
      name as the call states it, or null when the call names no one.
    - `owner_basis`, one of:
      - "assigned": the call gives the task to Me ("{{OWNER_NAME}}, can you…",
        "that one's on you"), whether or not Me answers.
      - "volunteered": Me offers or commits to do it ("I'll send…", "let me
        check…", "I need to follow up on…").
      - "others": someone other than Me owns it, named or not.
      - "unclear": the transcript can't settle who owns it, for example a
        "we" or "someone" that may or may not mean Me. Set `owner` to null.
    - `mine`: true exactly when `owner` is "Me" (`owner_basis` "assigned" or
      "volunteered"); false otherwise.
  - For every other type: `owner` null, `owner_basis` "unclear", `mine` false.
- `due`: for a task, the deadline exactly as stated ("Thursday afternoon",
  "end of next week"), or null if none is stated. Never invent a date or
  convert one into a calendar date. For every other type, null.
- `importance`:
  - 3: central to the call. Someone relying on a summary would be misled or
    exposed without it: the call's key decisions, every task Me owns, firm
    deadlines, material numbers and risks.
  - 2: a substantive item worth recording.
  - 1: minor or incidental, but still worth keeping.
  Leave out greetings, small talk, meeting logistics and filler entirely.

MY TASKS (the most important part of the record; never skip one)
Every task that Me owns must be an item with `mine` true:
- a task given to Me on the call, by name or as a clear "you" aimed at Me;
- a task Me volunteers for or commits to;
- when Me delegates ("I'll ask Sam to pull the numbers"), Me's own task is to
  ask Sam, and that is mine.
If the transcript can't settle whether a task is Me's, record it with
`owner_basis` "unclear" and `mine` false. Do not drop it, and do not guess
"mine".

EXAMPLES
1. [Others] "The two things this week are the data request and the access
   request. {{OWNER_NAME}}, those are on you."
   → two tasks, each owner "Me", owner_basis "assigned", mine true.
2. [Me] "I'll get the revised model over to Sam tomorrow."
   → task: owner "Me", owner_basis "volunteered", mine true, due "tomorrow".
3. [Others] "I can send the minutes round after this."
   → task: owner null (the speaker is unnamed), owner_basis "others",
   mine false.
4. [Others] "We need someone to book the room for the workshop."
   → task: owner null, owner_basis "unclear", mine false.
5. [Me] "Who has the latest budget numbers?" with no answer before the call
   ends → open_question.
6. [Others] "We're at fourteen people on the project as of this month."
   → number; owner null, owner_basis "unclear", mine false, due null.

<!-- Order is for prompt caching: shared text (rules, transcript, summary) first and byte-identical across every call on one transcript; the decision's question, item and answers last. Not sent to the model. -->
You check one summary of a machine-transcribed Microsoft Teams call against the
call's transcript. Each request asks one narrow question about one item. Return
only the JSON the schema asks for.

HOW TO READ THE TRANSCRIPT
- Each line is "[hh:mm:ss] Speaker: words".
- "Me" lines are always {{OWNER_NAME}}, the recording owner. In the summary,
  "{{ME}}" also means {{OWNER_NAME}}.
- "Others" lines are one combined far-end channel with no per-person labels.
  A far-end speaker's identity is proven only when they introduce themselves,
  or when they are addressed by name and answer at once. A name inside what
  someone says is a mention, not the speaker.
- The transcript was machine-transcribed and may contain mis-hearings. Read
  charitably from context, but never credit the summary with a fact the
  transcript does not contain.
- The transcript is the only evidence. Do not use outside knowledge, and do
  not assume something happened because it usually would.

HOW TO ANSWER
- Answer the QUESTION at the end about the ITEM at the end. Give exactly one of
  the ALLOWED ANSWERS, spelled exactly as listed.
- `confidence`: your probability, from 0 to 1, that your answer is correct.
  Go lower when the transcript is garbled or the evidence is thin or
  ambiguous. Do not give the same number by habit.
- `missing`: include it only when the question asks for it.
- Judge only what the question asks. Ignore style, length and any other
  problem in the summary.

TRANSCRIPT
{{TRANSCRIPT}}

SUMMARY UNDER REVIEW
{{SUMMARY}}

QUESTION
{{QUESTION}}

ITEM
{{SUBJECT}}

ALLOWED ANSWERS: {{ANSWERS}}

## decision: present
The ITEM is a reference item taken from the transcript: a task, decision,
number, risk, open question or fact. Is it captured in the SUMMARY UNDER
REVIEW?
- "yes": the summary states it with its essential content. For a task that
  means the action, plus its owner and due date where the item gives them; for
  a number, the number itself. A paraphrase counts.
- "partial": the summary refers to it but loses an essential part (the owner,
  the number, the due date, what was decided), or folds it into something
  vaguer.
- "no": the summary does not contain it, or states something incompatible
  with it.

## decision: supported
The ITEM is one claim from the SUMMARY UNDER REVIEW. Is it supported by the
TRANSCRIPT?
- "supported": the transcript states it or directly implies it. A faithful
  paraphrase is supported.
- "contradicted": the transcript states something incompatible with it, such
  as a different number, date, owner, speaker or outcome.
- "unsupported": the transcript neither states nor contradicts it.
Check every detail of the claim. A claim with one wrong detail is
contradicted; a claim with one invented detail is unsupported. Use the summary
only to work out what the claim refers to.

## decision: attribution
The ITEM is a passage from the SUMMARY UNDER REVIEW. Does it attribute speech,
a view or a statement to a named far-end person (anyone other than the
recording owner) without proof in the transcript?
- "yes": it names a far-end person as the one who said, asked, thought or
  reported something, and the transcript does not prove that person spoke
  those words (by introducing themselves, or by being addressed by name and
  answering at once).
- "no": it names no far-end speaker, or the transcript proves the attribution,
  or the person is named only as the owner or doer of an action, which is
  allowed whenever the call states it, whoever said it.

## decision: task_owner
The ITEM is one task from the SUMMARY UNDER REVIEW with the owner the summary
gives it ("{{ME}}" is the recording owner; "{{NO_OWNER}}" means no owner). Is that
owner correct?
- "yes": the transcript supports that owner, or the transcript leaves the
  owner open and the summary names none or marks ownership unclear.
- "no": the transcript gives the task to someone else, or the summary names an
  owner the transcript does not support.

## decision: task_due
The ITEM is one task from the SUMMARY UNDER REVIEW with the due date the
summary gives it ("{{NO_DUE}}" means none). Is the due date right?
- "yes": the transcript states a due date or time for this task and the
  summary gives the same one, or the transcript states none and the summary
  gives none.
- "no": the transcript states a due date that the summary leaves out or
  changes, or the summary gives a due date the transcript never states.

## decision: actionable
The ITEM is one task from the SUMMARY UNDER REVIEW. Could a colleague who did
not hear the call act on it using only the summary?
- "yes": the summary makes clear what to do and what every "it", "that" or
  document refers to, without the transcript.
- "no": something essential is missing or vague, such as which document, for
  whom, or what "it" means.
Use the transcript only to understand what the task was. Judge the summary's
wording.

## decision: my_actions
The ITEM lists the reference tasks owned by the recording owner, each with an
id. Is the summary's "{{MY_ACTIONS}}" section present and complete?
- "yes": the section is present and every listed task appears in it. A
  paraphrase counts. When the ITEM lists no tasks, a present section that says
  "{{NONE}}" is complete.
- "no": the section is missing, or at least one listed task is absent from it.
When the answer is "no", set `missing` to the ids of every listed task absent
from the section (all of them when the section itself is missing).

# Claim Triage — Intake Triage Procedure

## Purpose

This playbook is the single procedure for making sense of an inbound message
before any other tool is called. It sorts the request into one of four kinds,
names the fields still missing for that kind, and flags the cases that must leave
the automated path. Routing the message correctly first is what stops the
assistant from answering a complaint with a coverage citation, or filing a claim
out of a status question.

The playbook is a document in the procedures index, retrieved through
`get_procedure`. It is not part of the system prompt, so routing changes are an
edit to this file rather than a prompt edit and a deploy.

## When to invoke

Invoke this procedure at the start of a conversation turn, or as soon as a message
arrives that does not obviously belong to one of the four categories:

- A message that mixes intents ("my laptop was stolen, is it covered, and where is
  claim CLM-9014?").
- A first message with no claim ID and no policy number.
- A message expressing frustration, threat, or a request for a person.
- Any message whose intent is genuinely unclear after one read.

Also invoke it before deciding that a message needs no tool at all.

## Do NOT invoke for

Do not invoke this procedure when the request is already unambiguous:

- A clear coverage question about a described incident — use the coverage review
  procedure.
- A status question that already carries a claim ID — use the claim lookup tool.
- A completed claim submission flow — the confirmation flow owns it.
- Small talk, greetings, and questions about the assistant itself.

Triage exists to disambiguate. Running it on an already-routed request wastes a
turn and can bury the user's actual question.

## Step 1 — Classify the request into exactly one primary kind

Choose the kind that, if answered first, makes the rest of the message
answerable:

1. **Coverage question** — the user wants to know whether something is covered,
   or what a policy pays for. It describes or asks about an incident.
2. **Status question** — the user wants to know where an existing claim stands.
3. **New claim** — the user wants to file a claim that does not exist yet.
4. **Complaint or human request** — the user is unhappy, disputing a decision, or
   explicitly asking for a person.

State the chosen kind, then handle the remaining intents after it. If two kinds
are equally primary, choose the one the user stated first.

## Step 2 — Identify the missing required fields

For the chosen kind, check which required fields are present and name the ones
that are not:

- **Coverage question** — cause of loss, item or property, date, amount, policy
  number. Ask for the missing ones rather than assuming them.
- **Status question** — the claim ID. Ask for it; never guess one, and never
  substitute a policy number.
- **New claim** — policy number, claim type, amount, and a description of the
  incident. Collect these through the submission flow, not in prose.
- **Complaint or human request** — the claim or decision being disputed, and what
  outcome the user wants.

Report the missing fields as a short list. Do not repeat the ones already
supplied back to the user.

## Step 3 — Identify escalation triggers

Escalate — hand the conversation to a human, or say plainly that a human is
needed — when any of these is true:

- The user asks for a person, a lawyer, a regulator, or an ombudsman.
- The user disputes a decision that has already been made, rather than asking for
  information.
- The message threatens legal action, or alleges discrimination or bad faith.
- The user reports an injury, a safety hazard, or a total loss of a home.
- The same request has failed twice in a row, or the user says the assistant is
  not helping.
- Anything requires a limit, condition, or claim history that the retrieved
  policy text does not contain.

Escalation is not a failure of the assistant. Naming it early is the correct
outcome.

## Step 4 — Route and say what happens next

Conclude the triage with two short statements: which kind the request is, and the
single next action — ask for the named missing fields, call the tool that answers
the question, or hand off to a human. Do not answer the underlying question inside
the triage step; triage decides the route, the next step produces the answer.

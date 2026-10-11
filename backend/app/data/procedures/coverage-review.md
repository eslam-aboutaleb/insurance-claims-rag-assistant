# Coverage Review — Coverage Determination Procedure

## Purpose

This playbook is the single procedure for deciding whether a described incident is
COVERED, NOT COVERED, or UNDETERMINED under an OmniCare policy. It exists so every
coverage answer follows the same evidence trail: incident facts first, then the
policy text, then exactly one decision carrying a citation.

The playbook is a document in the procedures index, retrieved through
`get_procedure`. It is deliberately not part of the system prompt: a compliance
change is an edit to this file, not a prompt edit and not a deploy.

## When to invoke

Invoke this procedure whenever the user describes a concrete incident, or asks
about one, and needs a coverage determination:

- "My $3,000 laptop was stolen — am I covered?"
- "Is water damage from a burst pipe covered?"
- "A tree fell on my roof during the storm; what does my policy pay for?"

Also invoke it whenever a coverage decision is about to be stated, even when the
first policy chunk already looks like an answer. The checklist is what keeps the
answer honest.

## Do NOT invoke for

Do not invoke this procedure when the request is only:

- Claim status or history — use the claim lookup tools instead.
- Filing a new claim — use the claim submission flow; this procedure explains a
  determination, it does not file anything.
- General policy education with no incident ("what is a deductible?").
- Complaints, billing disputes, cancellations, or requests for a human — use the
  claim triage procedure.
- Any question whose answer requires a limit, exclusion, or condition that is not
  present in the retrieved policy text.

## Step 1 — Extract the incident facts

Before touching the policy, list the facts you actually have, each labelled as
stated by the user or unknown:

- **Cause of loss** — what happened, in the user's own words.
- **Item or property involved** — the thing that was damaged, lost, or stolen.
- **Date of the incident** — when it happened, and whether it was sudden or
  gradual.
- **Amount at stake** — the claimed value, repair cost, or item price.
- **Policy number** — the policy the claim would fall under.

Never fill a gap with an assumption. A missing fact stays missing and is what
makes the determination UNDETERMINED.

## Step 2 — Retrieve the policy context

Run a policy search for the cause of loss and the item class, and read the
returned sections in full. Every number you later use — a coverage cap, a
deductible, a per-item appraisal threshold, a listed exclusion — must come from
that retrieved text, together with the section it came from. Do not carry a limit
in from memory, from this playbook, or from another claim.

## Step 3 — Apply the checklist

Work the checks in order and record the outcome of each one:

1. **Covered peril.** Does the policy name this cause of loss as covered? Sudden
   and accidental causes are typically named; gradual or expected ones typically
   are not.
2. **Covered class.** Is the item in a class the policy covers? General personal
   property coverage and a specific scheduled-item endorsement are not the same
   thing.
3. **Per-item appraisal threshold.** Does the single item's value exceed the
   threshold at which an individual appraisal and receipt are required? If it
   does, that requirement is part of the answer, not a footnote.
4. **Coverage cap.** Does the amount at stake exceed the cap for this class of
   loss, including any single-item sublimit inside it?
5. **Required conditions.** Are the conditions the policy attaches to the
   coverage met — for example a deductible, a reporting window, or proof of loss?

Any check answered NO or UNKNOWN decides the outcome.

## Step 4 — Decide exactly one outcome

Pick exactly one and say so plainly:

- **COVERED** — every check passed and the policy text supports it.
- **NOT COVERED** — at least one check failed on explicit policy text, and that
  text is quoted.
- **UNDETERMINED** — a required fact is missing, or the policy text retrieved
  does not address the situation. Name what is missing.

Never hedge between two outcomes, never offer a probability, and never invent a
limit to close a gap. An honest UNDETERMINED with a named missing fact is a
correct answer; a guessed limit is not.

## Step 5 — Cite and close

End the determination with:

1. The decision word, exactly one of COVERED, NOT COVERED, or UNDETERMINED.
2. The policy section the decision rests on, quoted or closely paraphrased.
3. Any condition the policyholder still has to satisfy — appraisal, receipt,
   deductible, reporting deadline.
4. What would change the answer, when the decision is UNDETERMINED.

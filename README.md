# Lesson markup with an LLM (Google Docs round-trip)

A pipeline that lets a teacher write a lesson as plain text in a **Google Doc**,
then uses an **LLM to insert machine-readable markers** (H5P task types,
callouts, blocks) *into the same document*, with guard rails to make sure the
model never rewrites the teacher's text.

It is the pre-step of [`moodle-lesson-pipeline`](https://github.com/Andriy125/moodle-lesson-pipeline),
which turns the marked-up document into a Moodle lesson.

> Written on my own initiative while working in IT support — it was never part of my job description, I built it to remove repetitive manual work.

## The problem

Each lesson is a big table (one row per block). Columns are the roles of the
people who work on it: **COPYWRITER** (the text), **DESIGNER** (pictures),
**INTEGRATOR** (instructions for interactive tasks). Before the Moodle pipeline
can build H5P activities, the copywriter's text needs special markers in the
right places (`=FT=`, `=DT=`, `[[INSERT:...]]`, …). Adding them by hand across
dozens of lessons is slow and error-prone; asking an LLM to "mark it up" is
risky because LLMs love to paraphrase.

## How it works

```
generate_symbol.py     read the Doc via Google Docs API → lesson_raw.json
                       (text + formatting, tables, rich links, images)
create_user_input.py   split into one file per cell, grouped by section
gpt_async.py           async LLM call per cell with a section-specific prompt
                       (Claude via local `claude -p`, or Gemini API)
cells_to_actions.py    parse [[INSERT:...]] markers in the model output, compute their
                       absolute positions in the Doc → insert_actions.json
                       (+ deterministic rules that need no model)
change_Gdocs.py        apply the edits back to the Doc (descending index order)
qa_gate.py             rule-based checks of the result, no LLM
rollback_Gdocs.py      restore cells from the snapshot taken before the edit
build.py               runs the whole chain; --generate-only for a dry run
```

## Design decisions

- **The model may only add markers, never change text.** The global prompt
  forbids rephrasing. The model's output is not pasted back wholesale:
  `cells_to_actions.py` extracts the `[[INSERT:...]]` markers, computes their
  absolute positions in the live document, and `change_Gdocs.py` applies them
  as insertions at those positions.
- **Deterministic where possible.** Things that have a rule (dictionary
  callouts, iframe/title order, numbering) are done in code, not by the model.
  Only the judgement calls — "should this exercise become an H5P task, and of
  which type?" — go to the LLM.
- **Reversible.** Every run saves a snapshot; `rollback_Gdocs.py` rebuilds the
  changed cells from the original with their formatting (supports
  `--sections`, `--cell`, `--dry-run`) and refuses to run if the snapshot's
  `document_id` doesn't match the current lesson.
- **Fails closed.** If the model CLI is missing or no cell was processed, the
  run exits non-zero *before* anything is written to the document.
- **Two interchangeable backends** behind one `--backend` switch; the Gemini
  client is created lazily so no key is needed for the Claude backend.
- **Large prompts go through stdin**, because some section prompts exceed the
  Windows command-line limit.
- **Doc edits are applied in descending index order**, so earlier insertions
  don't shift the positions of later ones.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
cp info.example.json info.json     # lesson code, Google Doc id, service-account path
```

Share the Google Doc with the service account's e-mail (editor access).
Prompts are in `prompts/`; a subfolder named after the subject code
(e.g. `prompts/XX1/`) overrides the defaults for that subject.

## Usage

```bash
python build.py --generate-only      # produce markup, do NOT touch the Doc
python build.py                      # full run, writes markers into the Doc
python build.py --sections PRACTICA  # only some sections
python build.py --cell 19            # only one row
python qa_gate.py                    # rule-based checks (exit 1 on errors)
python rollback_Gdocs.py --dry-run   # preview an undo
```

## Tests

```bash
pytest
```

Covers the model-output cleaner, CLI discovery and the rollback logic
(colour/style conversion, offsets, table-cell lookup).

## Notes

- Prompts and section names are in Ukrainian and written for one school's
  lesson template; the architecture (parse → per-cell LLM → diff → guarded
  write-back → rollback) is reusable.
- The Docs-writing step is intentionally conservative; always try
  `--generate-only` on a copy of a document first.

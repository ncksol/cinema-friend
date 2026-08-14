# Seat Selection Mode Hints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Explain Simple and Advanced seat selection in the Telegram wizard before the user chooses a mode.

**Architecture:** Keep the current wizard state machine and inline keyboard unchanged. Extend only `_seat_mode_prompt`'s HTML text and lock the copy down in the existing callback-driven wizard test.

**Tech Stack:** Python 3.12, python-telegram-bot, pytest, pytest-asyncio, Ruff

## Global Constraints

- Keep the existing **Simple** and **Advanced** button labels and callback data unchanged.
- Render exactly one short explanatory line for each mode.
- Simple copy: `choose a preset for central seats between the aisles.`
- Advanced copy: `set preferred and excluded rows or exact seats yourself.`
- Do not change wizard state transitions, persistence, validation, seat matching, or errors.

## File Structure

- `src/cinema_friend/telegram/wizard.py`: renders the existing seat-mode prompt and buttons.
- `tests/telegram/test_wizard.py`: verifies the prompt copy, draft state, and callback data.

---

### Task 1: Explain the seat selection modes

**Files:**
- Modify: `tests/telegram/test_wizard.py:489-506`
- Modify: `src/cinema_friend/telegram/wizard.py:404-421`

**Interfaces:**
- Consumes: `handle_wizard_callback(update: Update, deps: WizardDeps) -> RenderedMessage | None`
- Produces: `_seat_mode_prompt() -> RenderedMessage` with explanatory HTML text and the unchanged `wizard:seat-mode:simple` and `wizard:seat-mode:advanced` callbacks.

- [ ] **Step 1: Write the failing prompt-copy assertion**

Add this assertion to
`test_new_quantity_choice_prompts_for_simple_or_advanced` after checking `reply`:

```python
assert reply.text == (
    "How would you like to choose acceptable seats?\n\n"
    "<b>Simple</b>: choose a preset for central seats between the aisles.\n"
    "<b>Advanced</b>: set preferred and excluded rows or exact seats yourself."
)
```

Keep the existing draft-state and callback-data assertions in the same test.

- [ ] **Step 2: Run the targeted test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py::test_new_quantity_choice_prompts_for_simple_or_advanced -q
```

Expected: FAIL because `reply.text` contains only
`How would you like to choose acceptable seats?`.

- [ ] **Step 3: Render the approved explanatory copy**

Replace `_seat_mode_prompt`'s `text` argument with:

```python
text=(
    "How would you like to choose acceptable seats?\n\n"
    "<b>Simple</b>: choose a preset for central seats between the aisles.\n"
    "<b>Advanced</b>: set preferred and excluded rows or exact seats yourself."
),
```

Do not alter the inline keyboard or callback data.

- [ ] **Step 4: Run the focused checks**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py -q
.venv/bin/python -m ruff check src/cinema_friend/telegram/wizard.py tests/telegram/test_wizard.py
```

Expected: all wizard tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit the implementation**

```bash
git add src/cinema_friend/telegram/wizard.py tests/telegram/test_wizard.py
git commit -m "feat: explain seat selection modes"
```

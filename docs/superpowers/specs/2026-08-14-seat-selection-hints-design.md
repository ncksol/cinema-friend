# Seat Selection Mode Hints

## Goal

Help users understand the difference between Simple and Advanced seat selection before
choosing a mode in the Telegram watch-creation wizard.

## Prompt Design

Keep the existing **Simple** and **Advanced** buttons unchanged. Expand the prompt above
them to read:

> How would you like to choose acceptable seats?
>
> **Simple**: choose a preset for central seats between the aisles.
>
> **Advanced**: set preferred and excluded rows or exact seats yourself.

Each explanation stays on one line so the two modes are easy to compare on a small screen.
The wording describes the existing behavior without introducing new concepts or changing
the subsequent prompts.

## Scope

Change only the rendered text from `_seat_mode_prompt` in
`src/cinema_friend/telegram/wizard.py`. Button labels, callback data, state transitions,
stored drafts, criteria validation, and seat matching remain unchanged.

No user-facing error behavior changes. Invalid or stale callbacks continue to use the
existing recoverable input errors.

## Testing

Extend the existing seat-mode wizard test to assert that the prompt explains both modes
and retains the current Simple and Advanced callbacks.

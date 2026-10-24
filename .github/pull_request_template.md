## What this changes

<!-- One paragraph. If the diff needs a paragraph of justification, say what forced it. -->

## Why

<!-- The behaviour before, the behaviour after, and what breaks if this does not land. -->

## How it was tested

<!--
Not "tests pass". Which command, and what it printed:

  make lint && make test
  ompwire gateway start telegram   -> ok   started and enabled
  send an image in the chat        -> arrives as an image block
-->

## Checklist

- [ ] `make lint` and `make test` are clean
- [ ] Behaviour change is reflected in `README.md` (and `ARCHITECTURE.md` if a boundary moved)
- [ ] No token, chat id or `.env` content in the diff, the tests or the screenshots
- [ ] New branch, no merge commits from `main` inside it
- [ ] One concern per PR; unrelated cleanup goes to its own PR

## Related

<!-- Issue number, or the command output that prompted this. -->

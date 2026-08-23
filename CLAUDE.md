# Project conventions

## Commit messages

Commit subjects become the release notes verbatim (one bullet per commit), so
they are user-facing changelog lines. Write them like changelog entries:

- Imperative, plain, specific: "Fix EEPROM wear from per-cycle current writes"
- One clause. No rhetorical flourishes, no "..., and ..." double-barrel titles
- Name the user-visible effect, not the implementation
- Keep under ~65 characters

Commit bodies stay for the *why*, but keep them tight: a short paragraph or
two, not an essay.

## Code comments and docs

Comments state constraints the code cannot show. Keep them short. Do not
narrate history or justify changes to a reviewer. README and user-facing text:
bullet points over prose, no marketing language.

## Releases

Bump the version in `custom_components/ess_controller/manifest.json` AND
`custom_components/ess_controller/const.py` (they must match the tag), run:

    ruff check custom_components tests
    ruff format --check custom_components tests
    python -m pytest tests/ -q

then commit and tag `vX.Y.Z`. The release workflow rejects mismatched
versions and builds release notes from commit subjects since the last tag.

## Tests

Fast suite: `python -m pytest tests/ -q` (no Home Assistant needed).
Full suite: `tests/test_with_homeassistant.py`, requires Home Assistant.
For behavioural fixes, verify the new test fails with the fix reverted.

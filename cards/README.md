# cards/

Character cards that ship with YuriOS.

## `yuri.png`

The canonical Yuri, exported from `soul-src/` at generation 0. Two things it is for:

- **something to import on a fresh install** — drop it on the switchboard (*Import*) and you
  have a working companion without writing one first;
- **a public reference file for the format** — it carries a full
  [`yurios` extension block](../docs/card-format.md), so anything claiming to read YuriOS cards
  has a real one to test against.

It is an ordinary [Character Card V3][ccv3] `.PNG` with a V2 fallback chunk, so it also opens in
SillyTavern, Chub, CharaVault and anything else that reads cards.

Nothing personal is on it, and nothing personal can be: a card carries who she is, never who you
are to her, and importing one starts the relationship at zero. See
[the card format](../docs/card-format.md#what-is-never-on-the-card).

## Regenerating it

Through the studio, deliberately, rather than by script: create a character from the template at
`/studio/`, export with the default options and *Include dates* off, and replace the file. A
script that re-cuts a shipped artefact on demand is a script that eventually re-cuts it from
whichever vault happened to be lying around.

*Include dates* off matters here — a shipped card should not carry the timestamp of the machine
that cut it.

[ccv3]: https://github.com/kwaroran/character-card-spec-v3

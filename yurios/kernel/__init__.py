"""The primitives every package depends on and none of them owns.

Injected time, the join key, and the one outbound bus began in `yurios/world`
because that is where the server lives and the server was what first needed
them. They outgrew it. Thirteen modules in `yurios/mind` and one in
`yurios/desktop` import all three, which left `mind → world` reading as a
layering violation in every import block — and it was not one: the clock, the
`corr_id` and the hub *are* the contracts the architecture is built on
(AGENTS.md's injected-time rule, one-event-bus rule, and one-`corr_id`-per-unit
-of-work rule). What was wrong was where they lived, not who imported them.

So: down here, below everything. `kernel` imports nothing from `yurios` — only
the standard library — and that is the property worth keeping. Anything that
grows a dependency on `world`, `app`, `mind` or `characters` does not belong in
this package, because the moment one does, every importer above it is back to
reaching sideways.

  clock.py      `Clock` / `VirtualClock` — no wall-clock reads in the mind
  correlate.py  `Origin` and one `corr_id` per unit of work
  hub.py        `EventHub` — every host→frontend event, typed, on one bus
"""

# Changes

## 0.2.0 (2026-11-10)

- Catches up after a reboot: containers with both a stop and a start time
  are put into the state their schedule says.
- Pause a container's schedule, or skip just its next time, from the web page.
- Recent activity on the web page, with problems in red.
- Optional alerts to Discord or ntfy when something goes wrong.
- Stops now go in reverse load order, so apps stop before their VPN or database.
- Fixed: load order 0 was treated as 100.
- Fixed: times between 01:00 and 02:00 ran twice when the clocks went back.
- Keeps running if Docker briefly drops out.
- Container healthcheck, so Unraid shows whether Controlarr is healthy.

## 0.1.0 (2026-11-03)

- First release: 24-hour stop, start and restart times, staggered starts,
  web page with light and dark modes.

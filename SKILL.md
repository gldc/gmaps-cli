---
name: gmaps
description: "Google Maps: search, ratings, hours, routes (gmaps CLI)."
---

# gmaps — Google Maps lookups from the shell

## When to Use

Use `gmaps` whenever you need real-world place data: finding businesses or POIs,
ratings and opening hours, converting addresses to coordinates (or back), or
travel time/distance between two points. Prefer it over scraping or guessing —
results come from the Google Maps Platform.

## Prerequisites

- `GMAPS_API_KEY` must be set in the environment (the only place the key is read
  from). If a command exits `3` with code `auth`, the key is missing or invalid.
- Python 3.11+ (`gmaps` is a single stdlib-only file).

## How to Run

- All flags are long-form. Both `--flag=value` and `--flag value` work; prefer
  the inline `--flag=value` form — it is unambiguous everywhere, including for
  negative coordinates (`--at=-34.6,-58.4`).
- Output is machine-parseable JSON: `{"ok": true, "data": ...}` on stdout, or
  `{"ok": false, "error": {code, message, retryable, hint}}` on stderr.
- Branch on exit codes, not text: `0` ok (zero results = `ok` with empty list) ·
  `2` usage · `3` auth/key · `4` not found · `5` upstream/network (**only 5 is
  worth retrying**).

## Quick Reference

```bash
gmaps search "trattoria" --near=43.07,11.68 --min-rating=4.3 --limit=5
gmaps nearby --near=45.50,-73.57 --type=pharmacy --rank=distance
gmaps place ChIJDbdkHFQayUwR7-8fITgxTmU            # details for one place id
gmaps place ChIJDbdkHFQayUwR7-8fITgxTmU --reviews  # + editorial summary, 3 reviews
gmaps geocode "Piazza del Campo, Siena"
gmaps revgeocode --at=45.5017,-73.5673
gmaps route --from="Rosemère, QC" --to="Trudeau Airport" --mode=drive
```

`route` waypoints accept an address, `LAT,LNG`, or `placeid:<id>`. Modes:
`drive` (default), `walk`, `bicycle`, `transit`.

## Cost Discipline

Field masks are fixed per command; the cheap path is:

1. `gmaps search "<query>"` first — id/name/address/location per result.
2. `gmaps place <id>` only for the one result you actually chose (adds rating,
   hours, phone, website).
3. `--detailed` on search/nearby when you need ratings across the whole list;
   `--reviews` only when the user explicitly wants review content.

## Pitfalls

- There are **no short flags** (`-o` etc.) — long-form only.
- `--min-rating` implies `--detailed` (the rating must be requested to filter on
  it).
- `transit` routes return duration/distance only (no itinerary).
- The CLI never retries; if exit is `5` and `retryable` is `true`, retry
  yourself with backoff.

## Verification

`gmaps geocode "Eiffel Tower"` should exit `0` and return one result near
`48.858, 2.294`. If it exits `3`, fix `GMAPS_API_KEY`; if `5`, check network and
retry.

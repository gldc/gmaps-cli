# gmaps-cli

A zero-dependency Google Maps CLI, built for AI agents and humans alike.
Wraps Places API (New), Geocoding v4, and Routes in a single stdlib-only
Python file with agent-friendly JSON output, fixed field masks (predictable
billing SKUs), and stable exit codes.

**Status: work in progress** — implementation landing shortly.

## Planned commands

```
gmaps search <query>        # Places Text Search (ratings/hours via --detailed)
gmaps nearby --near=LAT,LNG # Places Nearby Search
gmaps place <place-id>      # Place Details
gmaps geocode <address>     # forward geocode
gmaps revgeocode <LAT,LNG>  # reverse geocode
gmaps route --from=A --to=B # travel time/distance (drive/walk/bicycle/transit)
```

## Design principles

- **Zero dependencies** — single file, Python 3.11+ stdlib only.
- **Agent-optimized output** — compact JSON envelope `{"ok": true, "data": ...}` on
  stdout; errors as `{"ok": false, "error": {code, message, retryable, hint}}` on
  stderr; stable exit codes (0 ok · 2 usage · 3 auth · 4 not-found · 5 upstream).
- **Long-form flags only** — no short flags, no config file, no endpoint overrides.
- **Cost-deliberate** — fixed per-command field masks keep requests in the cheapest
  Google Maps Platform SKU that answers the question.
- The API key comes only from the `GMAPS_API_KEY` environment variable — never
  from argv.

## License

MIT

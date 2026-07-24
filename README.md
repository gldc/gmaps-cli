# gmaps-cli

A zero-dependency Google Maps CLI, built for AI agents and humans alike.
Wraps **Places API (New)**, **Geocoding v4**, and **Routes** in a single
stdlib-only Python file with agent-friendly JSON output, fixed field masks
(predictable billing SKUs), and stable exit codes.

```
gmaps search "espresso bar" --near=45.53,-73.61 --min-rating=4.3 --limit=5
gmaps route --from="Montréal, QC" --to="Québec City" --mode=drive
```

No SDK, no config file, no telemetry — just `python3` and one file.

## Install

The only runtime requirement is Python 3.11+. Pick whichever fits:

```bash
# Run the single file directly (nothing to install)
curl -fsSL https://raw.githubusercontent.com/gldc/gmaps-cli/main/gmaps.py -o gmaps.py
chmod +x gmaps.py
./gmaps.py search "ramen"

# Or install the console script with uv / pipx
uv tool install git+https://github.com/gldc/gmaps-cli
pipx install git+https://github.com/gldc/gmaps-cli
```

Either way the command reads your key from the `GMAPS_API_KEY` environment
variable (see [API key setup](#api-key-setup)).

## Commands

Every value flag accepts both `--flag=value` and `--flag value`. There are **no
short flags** and no config file — the endpoints are constants and the key comes
only from the environment.

```
gmaps search <query> [--near=LAT,LNG] [--radius=M] [--open-now] [--type=TYPE]
             [--min-rating=N] [--limit=N] [--detailed] [--lang=CODE] [--raw]
gmaps nearby --near=LAT,LNG [--radius=M] [--type=TYPE] [--limit=N]
             [--rank=popularity|distance] [--detailed] [--lang=CODE] [--raw]
gmaps place <place-id> [--reviews] [--lang=CODE] [--raw]
gmaps geocode <address> [--lang=CODE] [--raw]
gmaps revgeocode <LAT,LNG> | --at=LAT,LNG [--raw]
gmaps route --from=ORIGIN --to=DEST [--mode=drive|walk|bicycle|transit]
             [--lang=CODE] [--raw]
gmaps matrix --from=A [--from=B ...] --to=C [--to=D ...]
             [--mode=drive|walk|bicycle|transit] [--raw]
gmaps tz --at=LAT,LNG [--time=EPOCH] [--raw]
gmaps weather <place> | --at=LAT,LNG [--days=N | --hours=N]
             [--units=metric|imperial] [--lang=CODE] [--raw]
```

### `search` — Places Text Search

Free-text place search. Add `--near=LAT,LNG` to bias results toward a point
(`--radius` meters, default 5000, max 50000). `--type` takes a single
[place type](https://developers.google.com/maps/documentation/places/web-service/place-types)
(e.g. `restaurant`). `--min-rating` filters to N+ stars and implies `--detailed`.

```bash
gmaps search "third-wave coffee" --near=45.53,-73.61 --radius=1500 --open-now
gmaps search "trattoria" --type=restaurant --min-rating=4.4 --limit=5
```

`--detailed` (or `--min-rating`) enriches each result with rating, review count,
price level, open-now, phone, and website.

### `nearby` — Places Nearby Search

Everything of a type within a radius of a point. `--near` is **required**
(`--radius` default 1500, max 50000). `--rank=distance` orders by proximity;
`--rank=popularity` (the default upstream behavior) by prominence.

```bash
gmaps nearby --near=43.77,11.25 --type=restaurant --rank=distance --limit=10
gmaps nearby --near=45.50,-73.57 --type=pharmacy --detailed
```

### `place` — Place Details

Full detail for one place id (as returned by `search`/`nearby`/`geocode`).
`--reviews` adds an editorial summary and up to three review snippets.

```bash
gmaps place ChIJDbdkHFQayUwR7-8fITgxTmU
gmaps place ChIJDbdkHFQayUwR7-8fITgxTmU --reviews --lang=it
```

### `geocode` / `revgeocode` — address ↔ coordinates

```bash
gmaps geocode "1 Infinite Loop, Cupertino, CA"
gmaps geocode "Piazza del Campo, Siena" --lang=it
gmaps revgeocode --at=45.5017,-73.5673
gmaps revgeocode 45.5017,-73.5673            # positional form also works
```

Reverse geocode takes coordinates as `--at=LAT,LNG` or as a plain positional.
Prefer the `--at=` form in scripts and agent wrappers — a negative latitude as a
positional looks like a flag to some argv-validating wrappers, and `--at=` is
accepted everywhere.

### `route` — travel time and distance

`--from`/`--to` each accept a free-text address, `LAT,LNG`, or `placeid:<id>`.
Default mode is `drive`.

```bash
gmaps route --from="Rosemère, QC" --to="Trudeau Airport" --mode=drive
gmaps route --from=45.53,-73.61 --to=placeid:ChIJDbdkHFQayUwR7-8fITgxTmU --mode=transit
```

Distance and duration only — no polyline, no turn-by-turn (keeps output small
and the request in the cheapest SKU).

### `matrix` — many origins × many destinations

Repeat `--from` and `--to` (up to 10 each; billed per origin×destination
element). Same waypoint forms as `route`.

```bash
gmaps matrix --from="Home St, Rosemère" --from="Work Ave" --to="YUL Airport" --to="Downtown"
```

### `tz` — time zone at a coordinate

```bash
gmaps tz --at=45.5017,-73.5673
# {"ok":true,"data":{"timezone_id":"America/Toronto","utc_offset_s":-14400,...}}
```

`--time=EPOCH` evaluates DST for a specific moment (defaults to now).

### `weather` — current conditions or forecast

Takes a place name (geocoded first, one extra Essentials call) or `--at=LAT,LNG`.

```bash
gmaps weather "Rosemère QC"                 # current conditions
gmaps weather --at=43.07,11.68 --days=5     # daily forecast (1-10)
gmaps weather "Pienza" --hours=12 --units=metric
```

> Note: the Time Zone and Weather services document only `?key=` query-param
> auth, so for those two calls the key is in the request URL (over TLS). It is
> still scrubbed from every output stream.

## Output & agent contract

Success goes to **stdout** as compact JSON; errors go to **stderr**. The shape is
stable so an agent can branch on it without scraping prose.

```jsonc
// stdout, exit 0
{"ok":true,"data":[ {"name":"...","address":"...","lat":45.5,"lng":-73.6,
                     "place_id":"ChIJ...","maps_url":"https://...","types":["cafe"]} ]}

// stderr, non-zero exit
{"ok":false,"error":{"code":"not_found","message":"...","retryable":false,
                     "hint":"Check that the place id or address is valid."}}
```

Places are trimmed to a flat object — `name, address, lat, lng, place_id,
maps_url, types` always, plus `rating, ratings_count, price, open_now, phone,
website, hours` when a detailed mask was used. Pass `--raw` to get the untrimmed
upstream JSON instead (still limited to the fields in the mask).

### Exit codes

| code | meaning | `retryable` |
|---|---|---|
| `0` | success (zero results is success with an empty list) | — |
| `2` | usage error (bad flag, malformed `LAT,LNG`, out-of-range value) | `false` |
| `3` | auth / key problem (missing key, Google 401/403) | `false` |
| `4` | not found (bad place id, unresolvable address, Google 400/404) | `false` |
| `5` | upstream / network / timeout (Google 429/5xx, DNS, connect, 10s timeout) | `true` |

`retryable: true` means the same call may succeed if repeated; the CLI does **no**
internal retries, so the retry policy is entirely the caller's. The HTTP timeout
is a fixed 10 seconds and there is no `--timeout` flag.

The resolved API key is sent only in the `X-Goog-Api-Key` request header. It
never appears in argv, a URL, stdout, or stderr — including error paths, where
the key is scrubbed from any message before it is printed.

## API key setup

`gmaps` reads the key from `GMAPS_API_KEY` and nowhere else:

```bash
export GMAPS_API_KEY="AIza..."
```

Create a key with `gcloud` and restrict it to exactly the APIs this tool uses —
Places (New), Routes, and Geocoding — so a leaked key cannot reach anything else:

```bash
# Enable the three backends
gcloud services enable \
  places.googleapis.com \
  routes.googleapis.com \
  geocoding-backend.googleapis.com

# Create an API-restricted key (prints keyString once — capture it safely)
gcloud services api-keys create \
  --display-name="gmaps-cli" \
  --api-target=service=places.googleapis.com \
  --api-target=service=routes.googleapis.com \
  --api-target=service=geocoding-backend.googleapis.com

# Read the key string back later if needed
gcloud services api-keys get-key-string KEY_ID --format='value(keyString)'
```

> `api-keys create` echoes the key string in its response — capture it through a
> pipe or `--format`, and never paste it into a shared terminal or commit it.

## Cost & field-mask philosophy

The Places and Routes APIs bill per request, and the **most expensive field you
request sets the SKU for the whole call**. `gmaps` fixes one field mask per
command in a single table in the source, so cost is a property of the command you
run — not of a `--fields` flag a caller can accidentally inflate. There is no
field-mask override flag.

| command | tier | why |
|---|---|---|
| `search`, `nearby` | Places Pro | id, name, address, location, types, maps URL |
| `search --detailed` / `--min-rating`, `nearby --detailed` | Places Enterprise | adds rating, price, open-now, phone, website |
| `place` | Places Enterprise | full contact + opening hours for one chosen place |
| `place --reviews` | Enterprise + Atmosphere | adds editorial summary + review snippets |
| `route` | Routes Essentials | distance + duration only; never sets `routingPreference` |
| `matrix` | Routes Essentials | billed per origin×destination element; never traffic-aware |
| `tz` | Time Zone Essentials | single lookup |
| `weather` | Weather Essentials | current or forecast; place form adds one Geocoding call |
| `geocode`, `revgeocode` | Geocoding Essentials | no field mask required |

Practical guidance for agents: start with `search` (Pro), only add `--detailed`
when you actually need ratings or hours, and only call `place` for a result the
user has chosen. `--reviews` is the most expensive path — request it explicitly.

> **Running this under an AI agent?** Ship [`SKILL.md`](SKILL.md) into your
> harness's skills directory — it teaches the command surface, the JSON/exit-code
> contract, and the cost discipline in the standard agent-skill format.

## Design principles

- **Zero dependencies** — a single file, Python 3.11+ stdlib only.
- **Agent-optimized output** — compact JSON envelope on stdout, structured errors
  on stderr, stable exit codes, `retryable` on every error.
- **Long-form flags only** — no short flags, no config file, no endpoint or
  field-mask overrides.
- **Cost-deliberate** — fixed per-command field masks keep each request in the
  cheapest Google Maps Platform SKU that answers the question.
- **Key stays in the environment** — read only from `GMAPS_API_KEY`, sent only as
  a request header, scrubbed from every output stream.

## Development

```bash
uv sync --group dev   # or: python -m pip install pytest
python -m pytest -q
```

The test suite is hermetic: it injects a fake transport and asserts the exact
request each command builds (URL, headers, body, field mask) plus the output
trimming, exit-code map, and key redaction — no network access required. CI runs
it on Python 3.11 and 3.13.

## License

MIT

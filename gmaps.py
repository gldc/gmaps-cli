#!/usr/bin/python3 -I
"""gmaps — a zero-dependency Google Maps CLI for AI agents and humans.

Wraps the Places API (New), Geocoding v4, and Routes API in a single stdlib-only
Python file. Output is a compact JSON envelope on stdout; errors are a JSON
envelope on stderr with stable exit codes. The API key is read only from the
``GMAPS_API_KEY`` environment variable and sent via the ``X-Goog-Api-Key``
header — it never appears in argv, a URL, or any output stream.

Design notes:

* Field masks are fixed per subcommand (see ``FIELD_MASKS``). The mask's highest
  field determines the billing SKU, so pinning them keeps costs predictable and
  makes a cost regression a failing test rather than a surprise invoice.
* The HTTP layer is a single injectable callable ``transport(method, url,
  headers, body) -> (status, bytes)`` so the whole surface is testable without
  a network.
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
# Endpoints (hard-coded — there is deliberately no --url override flag).
# --------------------------------------------------------------------------- #
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_DETAILS_BASE = "https://places.googleapis.com/v1/places/"
GEOCODE_FWD_BASE = "https://geocode.googleapis.com/v4/geocode/address/"
GEOCODE_REV_BASE = "https://geocode.googleapis.com/v4/geocode/location/"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

TIMEOUT = 10  # seconds; kept below any supervising process's kill timeout.

# --------------------------------------------------------------------------- #
# Field masks — one table, the single source of truth for request cost.
# The highest-tier field in a mask sets the SKU, so keep these minimal.
# --------------------------------------------------------------------------- #
_SEARCH_BASE = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.location,places.types,places.googleMapsUri"
)
_SEARCH_DETAIL_EXTRA = (
    "places.rating,places.userRatingCount,places.priceLevel,"
    "places.currentOpeningHours.openNow,places.websiteUri,"
    "places.nationalPhoneNumber"
)
_PLACE_BASE = (
    "id,displayName,formattedAddress,location,types,googleMapsUri,"
    "rating,userRatingCount,priceLevel,websiteUri,nationalPhoneNumber,"
    "internationalPhoneNumber,regularOpeningHours,currentOpeningHours"
)
_PLACE_REVIEW_EXTRA = "editorialSummary,reviews"

FIELD_MASKS = {
    "search": _SEARCH_BASE,
    "search_detailed": _SEARCH_BASE + "," + _SEARCH_DETAIL_EXTRA,
    "nearby": _SEARCH_BASE,
    "nearby_detailed": _SEARCH_BASE + "," + _SEARCH_DETAIL_EXTRA,
    "place": _PLACE_BASE,
    "place_reviews": _PLACE_BASE + "," + _PLACE_REVIEW_EXTRA,
    "route": "routes.distanceMeters,routes.duration,routes.description",
}

DEFAULT_LIMIT = 8
SEARCH_DEFAULT_RADIUS = 5000
NEARBY_DEFAULT_RADIUS = 1500
MAX_RADIUS = 50000


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class GmapsError(Exception):
    """A structured, renderable error. ``exit_code`` drives the process code."""

    def __init__(self, code, message, retryable, hint, exit_code):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.hint = hint
        self.exit_code = exit_code


class _UsageError(Exception):
    """Raised by the argument parser instead of printing + calling exit()."""


class _JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of writing argparse's own text, so the
    JSON envelope is the only thing a caller ever sees on stderr."""

    def error(self, message):  # noqa: D401 - argparse hook
        raise _UsageError(message)


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def redact(text, secret):
    """Scrub the resolved API key from any text headed to a user stream.

    Scoped to the exact secret so legitimate values (place ids, hashes) survive.
    """
    if secret and len(secret) >= 4:
        text = text.replace(secret, "***")
    return text


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _emit_success(data, key):
    sys.stdout.write(redact(_dump({"ok": True, "data": data}), key) + "\n")


def _emit_error(exc, key):
    body = {
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
        "hint": exc.hint,
    }
    sys.stderr.write(redact(_dump({"ok": False, "error": body}), key) + "\n")
    return exc.exit_code


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _urlopen_transport(method, url, headers, body):
    """Default transport. Returns (status, bytes) for any HTTP response,
    including 4xx/5xx; raises URLError/timeout for genuine network failures."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _google_message(raw):
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            return msg
    return None


def _classify(status):
    """Map an HTTP error status to (code, exit_code, retryable, hint)."""
    if status in (401, 403):
        return (
            "auth",
            3,
            False,
            "Verify GMAPS_API_KEY is set and authorized for this API.",
        )
    if status in (400, 404):
        return (
            "not_found",
            4,
            False,
            "Check that the place id or address is valid.",
        )
    return (
        "upstream",
        5,
        True,
        "Transient upstream error; the request may be retried.",
    )


def _call(transport, method, url, headers, body_obj=None):
    hdrs = dict(headers)
    data = None
    if body_obj is not None:
        data = _dump(body_obj).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    try:
        status, raw = transport(method, url, hdrs, data)
    except (TimeoutError, socket.timeout):
        raise GmapsError(
            "timeout",
            "Request to Google Maps timed out",
            True,
            "Retry shortly.",
            5,
        )
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise GmapsError(
                "timeout",
                "Request to Google Maps timed out",
                True,
                "Retry shortly.",
                5,
            )
        raise GmapsError(
            "network",
            "Could not reach Google Maps",
            True,
            "Check connectivity and retry.",
            5,
        )
    if status == 200:
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise GmapsError(
                "upstream",
                "Malformed response from Google Maps",
                True,
                "Retry shortly.",
                5,
            )
    code, exit_code, retryable, hint = _classify(status)
    message = _google_message(raw) or f"Google Maps returned HTTP {status}"
    raise GmapsError(code, message, retryable, hint, exit_code)


# --------------------------------------------------------------------------- #
# Validation helpers (raise usage errors → exit 2)
# --------------------------------------------------------------------------- #
def _usage(message, hint):
    return GmapsError("usage", message, False, hint, 2)


def _parse_latlng(value, flag):
    parts = value.split(",")
    if len(parts) != 2:
        raise _usage(f"{flag} must be LAT,LNG", "Example: --near=45.51,-73.57")
    try:
        lat = float(parts[0])
        lng = float(parts[1])
    except ValueError:
        raise _usage(f"{flag} must be numeric LAT,LNG", "Example: --near=45.51,-73.57")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        raise _usage(
            f"{flag} is out of range",
            "Latitude -90..90, longitude -180..180.",
        )
    return lat, lng


def _validate_radius(radius):
    if radius < 1 or radius > MAX_RADIUS:
        raise _usage(
            f"--radius must be between 1 and {MAX_RADIUS} meters",
            "Example: --radius=2000",
        )
    return radius


def _validate_limit(limit):
    if limit < 1 or limit > 20:
        raise _usage("--limit must be between 1 and 20", "Example: --limit=5")
    return limit


def _validate_min_rating(rating):
    if rating <= 0 or rating > 5:
        raise _usage(
            "--min-rating must be greater than 0 and at most 5",
            "Example: --min-rating=4.0",
        )
    return rating


def _maybe_latlng(value):
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _waypoint(value):
    if value.startswith("placeid:"):
        return {"placeId": value[len("placeid:") :]}
    coords = _maybe_latlng(value)
    if coords is not None:
        lat, lng = coords
        return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
    return {"address": value}


# --------------------------------------------------------------------------- #
# Output trimming (token- and exfil-minimizing projections)
# --------------------------------------------------------------------------- #
def _set(out, key, value):
    if value is not None:
        out[key] = value


def _trim_place(place):
    out = {}
    _set(out, "name", (place.get("displayName") or {}).get("text"))
    _set(out, "address", place.get("formattedAddress"))
    loc = place.get("location") or {}
    _set(out, "lat", loc.get("latitude"))
    _set(out, "lng", loc.get("longitude"))
    _set(out, "place_id", place.get("id"))
    _set(out, "maps_url", place.get("googleMapsUri"))
    _set(out, "types", place.get("types"))
    _set(out, "rating", place.get("rating"))
    _set(out, "ratings_count", place.get("userRatingCount"))
    _set(out, "price", place.get("priceLevel"))
    _set(out, "open_now", (place.get("currentOpeningHours") or {}).get("openNow"))
    _set(
        out,
        "phone",
        place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"),
    )
    _set(out, "website", place.get("websiteUri"))
    _set(
        out,
        "hours",
        (place.get("regularOpeningHours") or {}).get("weekdayDescriptions"),
    )
    _set(out, "editorial", (place.get("editorialSummary") or {}).get("text"))
    reviews = place.get("reviews")
    if reviews is not None:
        out["reviews"] = [_trim_review(r) for r in reviews[:3]]
    return out


def _trim_review(review):
    out = {}
    _set(out, "author", (review.get("authorAttribution") or {}).get("displayName"))
    _set(out, "rating", review.get("rating"))
    text = (review.get("text") or {}).get("text") or (
        review.get("originalText") or {}
    ).get("text")
    _set(out, "text", text)
    _set(out, "when", review.get("relativePublishTimeDescription"))
    return out


def _trim_geocode(result):
    out = {}
    _set(out, "address", result.get("formattedAddress"))
    loc = result.get("location") or {}
    _set(out, "lat", loc.get("latitude"))
    _set(out, "lng", loc.get("longitude"))
    _set(out, "place_id", result.get("placeId"))
    _set(out, "granularity", result.get("granularity"))
    return out


def _duration_seconds(value):
    if isinstance(value, str) and value.endswith("s"):
        try:
            return int(value[:-1])
        except ValueError:
            return None
    return None


def _trim_route(route):
    out = {}
    _set(out, "distance_m", route.get("distanceMeters"))
    duration = route.get("duration")
    if duration is not None:
        seconds = _duration_seconds(duration)
        if seconds is not None:
            out["duration_s"] = seconds
        else:
            out["duration"] = duration
    _set(out, "summary", route.get("description"))
    return out


# --------------------------------------------------------------------------- #
# Subcommand handlers — each returns the ``data`` payload for the envelope.
# --------------------------------------------------------------------------- #
def cmd_search(args, key, transport):
    detailed = args.detailed or args.min_rating is not None
    mask = FIELD_MASKS["search_detailed" if detailed else "search"]
    body = {"textQuery": args.query, "pageSize": _validate_limit(args.limit)}
    if args.lang:
        body["languageCode"] = args.lang
    if args.type:
        body["includedType"] = args.type
    if args.min_rating is not None:
        body["minRating"] = _validate_min_rating(args.min_rating)
    if args.open_now:
        body["openNow"] = True
    radius = _validate_radius(
        args.radius if args.radius is not None else SEARCH_DEFAULT_RADIUS
    )
    if args.near:
        lat, lng = _parse_latlng(args.near, "--near")
        body["locationBias"] = {
            "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius}
        }
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": mask}
    resp = _call(transport, "POST", PLACES_SEARCH_URL, headers, body)
    if args.raw:
        return resp
    return [_trim_place(p) for p in resp.get("places", [])]


def cmd_nearby(args, key, transport):
    mask = FIELD_MASKS["nearby_detailed" if args.detailed else "nearby"]
    lat, lng = _parse_latlng(args.near, "--near")
    radius = _validate_radius(
        args.radius if args.radius is not None else NEARBY_DEFAULT_RADIUS
    )
    body = {
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius}
        },
        "maxResultCount": _validate_limit(args.limit),
    }
    if args.type:
        body["includedTypes"] = [args.type]
    if args.rank:
        body["rankPreference"] = (
            "POPULARITY" if args.rank == "popularity" else "DISTANCE"
        )
    if args.lang:
        body["languageCode"] = args.lang
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": mask}
    resp = _call(transport, "POST", PLACES_NEARBY_URL, headers, body)
    if args.raw:
        return resp
    return [_trim_place(p) for p in resp.get("places", [])]


def cmd_place(args, key, transport):
    mask = FIELD_MASKS["place_reviews" if args.reviews else "place"]
    url = PLACES_DETAILS_BASE + urllib.parse.quote(args.place_id, safe="")
    if args.lang:
        url += "?" + urllib.parse.urlencode({"languageCode": args.lang})
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": mask}
    resp = _call(transport, "GET", url, headers, None)
    if args.raw:
        return resp
    return _trim_place(resp)


def cmd_geocode(args, key, transport):
    url = GEOCODE_FWD_BASE + urllib.parse.quote(args.address, safe="")
    if args.lang:
        url += "?" + urllib.parse.urlencode({"languageCode": args.lang})
    headers = {"X-Goog-Api-Key": key}
    resp = _call(transport, "GET", url, headers, None)
    if args.raw:
        return resp
    return [_trim_geocode(r) for r in resp.get("results", [])]


def cmd_revgeocode(args, key, transport):
    coords = args.at_flag if args.at_flag is not None else args.at
    if not coords:
        raise _usage(
            "revgeocode requires LAT,LNG via --at or a positional argument",
            "Example: gmaps revgeocode --at=45.51,-73.57",
        )
    _parse_latlng(coords, "--at")
    url = GEOCODE_REV_BASE + urllib.parse.quote(coords, safe=",-.")
    headers = {"X-Goog-Api-Key": key}
    resp = _call(transport, "GET", url, headers, None)
    if args.raw:
        return resp
    return [_trim_geocode(r) for r in resp.get("results", [])]


def cmd_route(args, key, transport):
    body = {
        "origin": _waypoint(args.origin),
        "destination": _waypoint(args.dest),
        "travelMode": args.mode.upper(),
    }
    if args.lang:
        body["languageCode"] = args.lang
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASKS["route"]}
    resp = _call(transport, "POST", ROUTES_URL, headers, body)
    if args.raw:
        return resp
    return [_trim_route(r) for r in resp.get("routes", [])]


# --------------------------------------------------------------------------- #
# Argument parser
# --------------------------------------------------------------------------- #
def _add_subparser(sub, name, help_text):
    parser = sub.add_parser(name, help=help_text, add_help=False)
    parser.add_argument("--help", action="help", help="show this help message and exit")
    return parser


def build_parser():
    parser = _JsonArgumentParser(
        prog="gmaps",
        description="Zero-dependency Google Maps CLI (Places New, Geocoding v4, Routes).",
        add_help=False,
    )
    parser.add_argument("--help", action="help", help="show this help message and exit")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_JsonArgumentParser,
        metavar="COMMAND",
    )

    sp = _add_subparser(sub, "search", "Places Text Search")
    sp.add_argument("query")
    sp.add_argument("--near", metavar="LAT,LNG")
    sp.add_argument("--radius", type=int, metavar="M")
    sp.add_argument("--open-now", action="store_true")
    sp.add_argument("--type", metavar="TYPE")
    sp.add_argument("--min-rating", type=float, metavar="N")
    sp.add_argument("--limit", type=int, default=DEFAULT_LIMIT, metavar="N")
    sp.add_argument("--detailed", action="store_true")
    sp.add_argument("--lang", metavar="CODE")
    sp.add_argument("--raw", action="store_true")
    sp.set_defaults(func=cmd_search)

    np = _add_subparser(sub, "nearby", "Places Nearby Search")
    np.add_argument("--near", required=True, metavar="LAT,LNG")
    np.add_argument("--radius", type=int, metavar="M")
    np.add_argument("--type", metavar="TYPE")
    np.add_argument("--limit", type=int, default=DEFAULT_LIMIT, metavar="N")
    np.add_argument("--rank", choices=["popularity", "distance"])
    np.add_argument("--detailed", action="store_true")
    np.add_argument("--lang", metavar="CODE")
    np.add_argument("--raw", action="store_true")
    np.set_defaults(func=cmd_nearby)

    pp = _add_subparser(sub, "place", "Place Details")
    pp.add_argument("place_id", metavar="PLACE_ID")
    pp.add_argument("--reviews", action="store_true")
    pp.add_argument("--lang", metavar="CODE")
    pp.add_argument("--raw", action="store_true")
    pp.set_defaults(func=cmd_place)

    gp = _add_subparser(sub, "geocode", "Forward geocode an address")
    gp.add_argument("address")
    gp.add_argument("--lang", metavar="CODE")
    gp.add_argument("--raw", action="store_true")
    gp.set_defaults(func=cmd_geocode)

    rp = _add_subparser(sub, "revgeocode", "Reverse geocode LAT,LNG")
    rp.add_argument("at", nargs="?", metavar="LAT,LNG")
    rp.add_argument("--at", dest="at_flag", metavar="LAT,LNG")
    rp.add_argument("--raw", action="store_true")
    rp.set_defaults(func=cmd_revgeocode)

    rtp = _add_subparser(sub, "route", "Travel time and distance")
    rtp.add_argument("--from", dest="origin", required=True, metavar="ORIGIN")
    rtp.add_argument("--to", dest="dest", required=True, metavar="DEST")
    rtp.add_argument(
        "--mode", choices=["drive", "walk", "bicycle", "transit"], default="drive"
    )
    rtp.add_argument("--lang", metavar="CODE")
    rtp.add_argument("--raw", action="store_true")
    rtp.set_defaults(func=cmd_route)

    return parser


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _reconfigure_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None, transport=None):
    _reconfigure_streams()
    if transport is None:
        transport = _urlopen_transport
    key = os.environ.get("GMAPS_API_KEY") or ""

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        return _emit_error(_usage(str(exc), "Run `gmaps --help` for usage."), key)

    if not key:
        return _emit_error(
            GmapsError(
                "auth",
                "GMAPS_API_KEY is not set",
                False,
                "Set the GMAPS_API_KEY environment variable.",
                3,
            ),
            key,
        )

    try:
        data = args.func(args, key, transport)
    except GmapsError as exc:
        return _emit_error(exc, key)

    _emit_success(data, key)
    return 0


if __name__ == "__main__":
    sys.exit(main())

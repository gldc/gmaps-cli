#!/usr/bin/python3 -I
"""gmaps — a zero-dependency Google Maps CLI for AI agents and humans.

Wraps the Places API (New), Geocoding v4, Routes API, Time Zone API, and
Weather API in a single stdlib-only Python file. Output is a compact JSON
envelope on stdout; errors are a JSON envelope on stderr with stable exit
codes. The API key is read only from the ``GMAPS_API_KEY`` environment
variable and sent via the ``X-Goog-Api-Key`` header where supported (Places,
Geocoding, Routes); the Time Zone and Weather services only document the
``?key=`` query-parameter form, so for those two the key rides the request URL
— it still never appears in argv or any output stream (all output is scrubbed
at the emit boundary).

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
import time
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
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json"
WEATHER_BASE = "https://weather.googleapis.com/v1/"

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
    # MUST include `status`: without it every matrix element reads as OK.
    "matrix": "originIndex,destinationIndex,status,condition,distanceMeters,duration",
}

DEFAULT_LIMIT = 8
WEATHER_MAX_DAYS = 10  # API maximum
WEATHER_MAX_HOURS = 24  # forecast/hours pageSize maxes at 24; no pagination
MATRIX_MAX_SIDE = 10  # 10x10 = 100 elements; API cap is 625 (100 for transit)
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
        encoded = urllib.parse.quote_plus(secret)
        if encoded != secret:
            text = text.replace(encoded, "***")
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
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, (dict, list)):
                raise ValueError("non-container JSON body")
            return parsed
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


def _cond_text(container):
    return ((container.get("weatherCondition") or {}).get("description") or {}).get(
        "text"
    )


def _trim_weather_current(resp):
    out = {}
    _set(out, "condition", _cond_text(resp))
    _set(out, "temp", (resp.get("temperature") or {}).get("degrees"))
    _set(out, "feels_like", (resp.get("feelsLikeTemperature") or {}).get("degrees"))
    _set(out, "humidity_pct", resp.get("relativeHumidity"))
    wind = resp.get("wind") or {}
    _set(out, "wind_kmh", (wind.get("speed") or {}).get("value"))
    _set(out, "wind_dir", (wind.get("direction") or {}).get("cardinal"))
    precip = ((resp.get("precipitation") or {}).get("probability")) or {}
    _set(out, "precip_prob_pct", precip.get("percent"))
    _set(out, "uv_index", resp.get("uvIndex"))
    _set(out, "cloud_pct", resp.get("cloudCover"))
    return out


def _weather_date(display_date):
    y = display_date.get("year")
    m = display_date.get("month")
    d = display_date.get("day")
    if y is None or m is None or d is None:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def _trim_weather_day(day):
    out = {}
    _set(out, "date", _weather_date(day.get("displayDate") or {}))
    daytime = day.get("daytimeForecast") or {}
    night = day.get("nighttimeForecast") or {}
    _set(out, "condition_day", _cond_text(daytime))
    _set(out, "condition_night", _cond_text(night))
    _set(out, "max", (day.get("maxTemperature") or {}).get("degrees"))
    _set(out, "min", (day.get("minTemperature") or {}).get("degrees"))
    _set(
        out,
        "precip_prob_pct",
        (((daytime.get("precipitation") or {}).get("probability")) or {}).get(
            "percent"
        ),
    )
    return out


def _trim_weather_hour(hour):
    out = {}
    _set(out, "time", hour.get("interval", {}).get("startTime"))
    _set(
        out,
        "condition",
        hour.get("weatherCondition", {}).get("description", {}).get("text"),
    )
    _set(out, "temp", hour.get("temperature", {}).get("degrees"))
    _set(
        out,
        "precip_prob_pct",
        hour.get("precipitation", {}).get("probability", {}).get("percent"),
    )
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


# Time Zone API is a legacy web service: HTTP 200 with an embedded status enum.
_TZ_STATUS_ERRORS = {
    "ZERO_RESULTS": (
        "not_found",
        "No time zone for this location",
        False,
        4,
        "Coordinates may be over open water.",
    ),
    "REQUEST_DENIED": (
        "auth",
        "Time Zone API request denied",
        False,
        3,
        "Check that the API key permits the Time Zone API.",
    ),
    "INVALID_REQUEST": (
        "usage",
        "Invalid Time Zone API request",
        False,
        2,
        "Check the --at coordinates.",
    ),
    "OVER_QUERY_LIMIT": (
        "rate_limited",
        "Time Zone API quota exceeded",
        True,
        5,
        "Retry later.",
    ),
    "OVER_DAILY_LIMIT": (
        "rate_limited",
        "Time Zone API daily quota exceeded",
        True,
        5,
        "Retry tomorrow or raise the quota cap.",
    ),
}


def cmd_tz(args, key, transport):
    if not args.at:
        raise _usage(
            "tz requires --at=LAT,LNG",
            "Example: gmaps tz --at=45.63,-73.78",
        )
    _parse_latlng(args.at, "--at")
    timestamp = args.time if args.time is not None else int(time.time())
    url = (
        TIMEZONE_URL
        + "?"
        + urllib.parse.urlencode(
            {"location": args.at, "timestamp": timestamp, "key": key}
        )
    )
    resp = _call(transport, "GET", url, {}, None)
    if not isinstance(resp, dict):
        raise GmapsError(
            "upstream", "Malformed Time Zone API response", True, "Retry shortly.", 5
        )
    status = resp.get("status")
    if status != "OK":
        code, message, retryable, exit_code, hint = _TZ_STATUS_ERRORS.get(
            status,
            (
                "upstream",
                f"Time Zone API returned status {status}",
                True,
                5,
                "Retry shortly.",
            ),
        )
        raise GmapsError(code, message, retryable, hint, exit_code)
    if args.raw:
        return resp
    raw_offset = resp.get("rawOffset", 0)
    dst_offset = resp.get("dstOffset", 0)
    return {
        "timezone_id": resp.get("timeZoneId"),
        "timezone_name": resp.get("timeZoneName"),
        "raw_offset_s": raw_offset,
        "dst_offset_s": dst_offset,
        "utc_offset_s": raw_offset + dst_offset,
    }


def _resolve_location(args, key, transport):
    """LAT,LNG strings for --at, else forward-geocode the positional place."""
    if args.at:
        lat, lng = _parse_latlng(args.at, "--at")
        raw_lat, raw_lng = args.at.split(",", 1)
        return raw_lat.strip(), raw_lng.strip()
    if args.place:
        url = GEOCODE_FWD_BASE + urllib.parse.quote(args.place, safe="")
        resp = _call(transport, "GET", url, {"X-Goog-Api-Key": key}, None)
        results = resp.get("results", [])
        if not results:
            raise GmapsError(
                "not_found",
                f"Could not geocode {args.place!r}",
                False,
                "Try a more specific place name or pass --at=LAT,LNG.",
                4,
            )
        loc = results[0].get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is None or lng is None:
            raise GmapsError(
                "not_found",
                f"Geocode result for {args.place!r} has no coordinates",
                False,
                "Try a more specific place name or pass --at=LAT,LNG.",
                4,
            )
        return str(lat), str(lng)
    raise _usage(
        "weather requires a place or --at=LAT,LNG",
        'Examples: gmaps weather "Rosemère QC" · gmaps weather --at=45.63,-73.78',
    )


def cmd_weather(args, key, transport):
    if args.days is not None and args.hours is not None:
        raise _usage(
            "--days and --hours are mutually exclusive",
            "Pick one forecast granularity.",
        )
    if args.days is not None and not 1 <= args.days <= WEATHER_MAX_DAYS:
        raise _usage(
            f"--days must be 1-{WEATHER_MAX_DAYS}",
            "Example: gmaps weather --at=45.63,-73.78 --days=3",
        )
    if args.hours is not None and not 1 <= args.hours <= WEATHER_MAX_HOURS:
        raise _usage(
            f"--hours must be 1-{WEATHER_MAX_HOURS}",
            "Example: gmaps weather --at=45.63,-73.78 --hours=6",
        )
    lat, lng = _resolve_location(args, key, transport)
    params = {"key": key, "location.latitude": lat, "location.longitude": lng}
    if args.days is not None:
        endpoint = "forecast/days:lookup"
        params["days"] = args.days
        params["pageSize"] = args.days
    elif args.hours is not None:
        endpoint = "forecast/hours:lookup"
        params["hours"] = args.hours
        params["pageSize"] = args.hours
    else:
        endpoint = "currentConditions:lookup"
    if args.units:
        params["unitsSystem"] = args.units.upper()
    if args.lang:
        params["languageCode"] = args.lang
    url = WEATHER_BASE + endpoint + "?" + urllib.parse.urlencode(params)
    resp = _call(transport, "GET", url, {}, None)
    if not isinstance(resp, dict):
        raise GmapsError(
            "upstream", "Malformed Weather API response", True, "Retry shortly.", 5
        )
    if args.raw:
        return resp
    if args.days is not None:
        return [_trim_weather_day(d) for d in resp.get("forecastDays", [])]
    if args.hours is not None:
        return [_trim_weather_hour(h) for h in resp.get("forecastHours", [])]
    return _trim_weather_current(resp)


def cmd_matrix(args, key, transport):
    origins = args.origins or []
    dests = args.dests or []
    if (
        not 1 <= len(origins) <= MATRIX_MAX_SIDE
        or not 1 <= len(dests) <= MATRIX_MAX_SIDE
    ):
        raise _usage(
            f"matrix needs 1-{MATRIX_MAX_SIDE} --from and 1-{MATRIX_MAX_SIDE} --to",
            'Example: gmaps matrix --from="Home St" --from="Work Ave" --to="Airport"',
        )
    body = {
        "origins": [{"waypoint": _waypoint(v)} for v in origins],
        "destinations": [{"waypoint": _waypoint(v)} for v in dests],
        "travelMode": args.mode.upper(),
    }
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASKS["matrix"]}
    resp = _call(transport, "POST", MATRIX_URL, headers, body)
    if not isinstance(resp, list):
        detail = None
        if isinstance(resp, dict):
            err = resp.get("error")
            if isinstance(err, dict):
                detail = err.get("message")
        raise GmapsError(
            "upstream",
            detail or "Unexpected Route Matrix response",
            True,
            "Retry shortly.",
            5,
        )
    if args.raw:
        return resp
    elements = sorted(
        resp,
        key=lambda e: (e.get("originIndex", 0), e.get("destinationIndex", 0)),
    )
    out = []
    for el in elements:
        row = {}
        oi, di = el.get("originIndex", 0), el.get("destinationIndex", 0)
        if oi < len(origins):
            row["from"] = origins[oi]
        if di < len(dests):
            row["to"] = dests[di]
        _set(row, "distance_m", el.get("distanceMeters"))
        _set(row, "duration_s", _duration_seconds(el.get("duration")))
        _set(row, "condition", el.get("condition"))
        el_status = el.get("status") or {}
        if el_status.get("code"):
            _set(row, "error", el_status.get("message") or "element failed")
        out.append(row)
    return out


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

    tzp = _add_subparser(sub, "tz", "Time zone at a coordinate")
    tzp.add_argument("--at", metavar="LAT,LNG")
    tzp.add_argument("--time", type=int, metavar="EPOCH")
    tzp.add_argument("--raw", action="store_true")
    tzp.set_defaults(func=cmd_tz)

    wp = _add_subparser(sub, "weather", "Current conditions or forecast")
    wp.add_argument("place", nargs="?", metavar="PLACE")
    wp.add_argument("--at", metavar="LAT,LNG")
    wp.add_argument("--days", type=int, metavar="N")
    wp.add_argument("--hours", type=int, metavar="N")
    wp.add_argument("--units", choices=["metric", "imperial"])
    wp.add_argument("--lang", metavar="CODE")
    wp.add_argument("--raw", action="store_true")
    wp.set_defaults(func=cmd_weather)

    mp = _add_subparser(
        sub, "matrix", "Distance/time for many origin-destination pairs"
    )
    mp.add_argument("--from", dest="origins", action="append", metavar="ORIGIN")
    mp.add_argument("--to", dest="dests", action="append", metavar="DEST")
    mp.add_argument(
        "--mode", choices=["drive", "walk", "bicycle", "transit"], default="drive"
    )
    mp.add_argument("--raw", action="store_true")
    mp.set_defaults(func=cmd_matrix)

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

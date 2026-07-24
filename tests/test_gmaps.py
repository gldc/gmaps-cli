"""Hermetic tests for the gmaps CLI.

No network: an injected transport records the exact request (method, url, headers,
body) each subcommand builds and returns a canned response. Covers the argparse
surface, the verbatim field-mask table, per-subcommand request shapes, output
trimming, the exit-code map, the JSON envelope, key-redaction, and Unicode.
"""

import json
import urllib.error

import pytest

import gmaps

API_KEY = "TESTKEY-abc123-super-secret"


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("GMAPS_API_KEY", API_KEY)


class Recorder:
    """Injectable transport: records calls, returns a canned (status, bytes)."""

    def __init__(self, status=200, payload=None, raw=None, exc=None):
        self.calls = []
        self.status = status
        self.payload = payload if payload is not None else {}
        self.raw = raw
        self.exc = exc

    def __call__(self, method, url, headers, body):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        if self.exc is not None:
            raise self.exc
        if self.raw is not None:
            return self.status, self.raw
        return self.status, json.dumps(self.payload).encode("utf-8")

    @property
    def last(self):
        return self.calls[-1]

    def body_json(self):
        b = self.last["body"]
        return json.loads(b.decode("utf-8")) if b else None


def ok(status=200, payload=None):
    return Recorder(
        status=status, payload=payload if payload is not None else {"places": []}
    )


def err(status, message="boom"):
    return Recorder(
        status=status, raw=json.dumps({"error": {"message": message}}).encode("utf-8")
    )


# --------------------------------------------------------------------------- #
# Field-mask table (verbatim, single equality — a cost-regression guard)
# --------------------------------------------------------------------------- #


def test_field_mask_table_is_verbatim():
    assert gmaps.FIELD_MASKS == {
        "search": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.types,places.googleMapsUri"
        ),
        "search_detailed": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.types,places.googleMapsUri,"
            "places.rating,places.userRatingCount,places.priceLevel,"
            "places.currentOpeningHours.openNow,places.websiteUri,"
            "places.nationalPhoneNumber"
        ),
        "nearby": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.types,places.googleMapsUri"
        ),
        "nearby_detailed": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.types,places.googleMapsUri,"
            "places.rating,places.userRatingCount,places.priceLevel,"
            "places.currentOpeningHours.openNow,places.websiteUri,"
            "places.nationalPhoneNumber"
        ),
        "place": (
            "id,displayName,formattedAddress,location,types,googleMapsUri,"
            "rating,userRatingCount,priceLevel,websiteUri,nationalPhoneNumber,"
            "internationalPhoneNumber,regularOpeningHours,currentOpeningHours"
        ),
        "place_reviews": (
            "id,displayName,formattedAddress,location,types,googleMapsUri,"
            "rating,userRatingCount,priceLevel,websiteUri,nationalPhoneNumber,"
            "internationalPhoneNumber,regularOpeningHours,currentOpeningHours,"
            "editorialSummary,reviews"
        ),
        "route": "routes.distanceMeters,routes.duration,routes.description",
        "matrix": (
            "originIndex,destinationIndex,status,condition,distanceMeters,duration"
        ),
    }


def test_nearby_masks_equal_search_masks():
    assert gmaps.FIELD_MASKS["nearby"] == gmaps.FIELD_MASKS["search"]
    assert gmaps.FIELD_MASKS["nearby_detailed"] == gmaps.FIELD_MASKS["search_detailed"]


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def test_search_basic_request():
    rec = ok()
    rc = gmaps.main(["search", "coffee"], transport=rec)
    assert rc == 0
    assert rec.last["method"] == "POST"
    assert rec.last["url"] == "https://places.googleapis.com/v1/places:searchText"
    h = rec.last["headers"]
    assert h["X-Goog-Api-Key"] == API_KEY
    assert h["X-Goog-FieldMask"] == gmaps.FIELD_MASKS["search"]
    assert h["Content-Type"] == "application/json"
    assert rec.body_json() == {"textQuery": "coffee", "pageSize": 8}


def test_search_type_is_singular_string():
    rec = ok()
    gmaps.main(["search", "food", "--type=restaurant"], transport=rec)
    body = rec.body_json()
    assert body["includedType"] == "restaurant"
    assert "includedTypes" not in body


def test_search_near_builds_location_bias():
    rec = ok()
    gmaps.main(
        ["search", "coffee", "--near=45.51,-73.57", "--radius=2000"], transport=rec
    )
    assert rec.body_json()["locationBias"] == {
        "circle": {"center": {"latitude": 45.51, "longitude": -73.57}, "radius": 2000}
    }


def test_search_near_default_radius_5000():
    rec = ok()
    gmaps.main(["search", "coffee", "--near=45.51,-73.57"], transport=rec)
    assert rec.body_json()["locationBias"]["circle"]["radius"] == 5000


def test_search_open_now_and_lang():
    rec = ok()
    gmaps.main(["search", "coffee", "--open-now", "--lang=fr"], transport=rec)
    body = rec.body_json()
    assert body["openNow"] is True
    assert body["languageCode"] == "fr"


def test_search_detailed_uses_detailed_mask():
    rec = ok()
    gmaps.main(["search", "coffee", "--detailed"], transport=rec)
    assert (
        rec.last["headers"]["X-Goog-FieldMask"] == gmaps.FIELD_MASKS["search_detailed"]
    )


def test_search_min_rating_implies_detailed():
    rec = ok()
    gmaps.main(["search", "coffee", "--min-rating=4"], transport=rec)
    assert (
        rec.last["headers"]["X-Goog-FieldMask"] == gmaps.FIELD_MASKS["search_detailed"]
    )
    assert rec.body_json()["minRating"] == 4.0


def test_search_limit_maps_to_pagesize():
    rec = ok()
    gmaps.main(["search", "coffee", "--limit=3"], transport=rec)
    assert rec.body_json()["pageSize"] == 3


def test_search_trim_output(capsys):
    payload = {
        "places": [
            {
                "id": "P1",
                "displayName": {"text": "Café X", "languageCode": "fr"},
                "formattedAddress": "1 rue Principale",
                "location": {"latitude": 45.5, "longitude": -73.5},
                "types": ["cafe", "food"],
                "googleMapsUri": "https://maps.google.com/?cid=1",
            }
        ]
    }
    rec = ok(payload=payload)
    gmaps.main(["search", "coffee"], transport=rec)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["data"] == [
        {
            "name": "Café X",
            "address": "1 rue Principale",
            "lat": 45.5,
            "lng": -73.5,
            "place_id": "P1",
            "maps_url": "https://maps.google.com/?cid=1",
            "types": ["cafe", "food"],
        }
    ]


def test_search_detailed_trim_includes_ratings(capsys):
    payload = {
        "places": [
            {
                "id": "P1",
                "displayName": {"text": "Bistro"},
                "formattedAddress": "2 rue",
                "location": {"latitude": 1.0, "longitude": 2.0},
                "types": ["restaurant"],
                "googleMapsUri": "u",
                "rating": 4.6,
                "userRatingCount": 231,
                "priceLevel": "PRICE_LEVEL_MODERATE",
                "currentOpeningHours": {"openNow": True},
                "websiteUri": "https://bistro.example",
                "nationalPhoneNumber": "514-555-0199",
            }
        ]
    }
    rec = ok(payload=payload)
    gmaps.main(["search", "bistro", "--detailed"], transport=rec)
    d = json.loads(capsys.readouterr().out)["data"][0]
    assert d["rating"] == 4.6
    assert d["ratings_count"] == 231
    assert d["price"] == "PRICE_LEVEL_MODERATE"
    assert d["open_now"] is True
    assert d["phone"] == "514-555-0199"
    assert d["website"] == "https://bistro.example"


def test_search_zero_results_is_ok_empty(capsys):
    rec = ok(payload={"places": []})
    rc = gmaps.main(["search", "nothing here"], transport=rec)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "data": []}


def test_search_zero_results_missing_key(capsys):
    rec = ok(payload={})  # no "places" key at all
    rc = gmaps.main(["search", "x"], transport=rec)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["data"] == []


# --------------------------------------------------------------------------- #
# nearby
# --------------------------------------------------------------------------- #


def test_nearby_basic_request():
    rec = ok()
    rc = gmaps.main(["nearby", "--near=45.5,-73.6"], transport=rec)
    assert rc == 0
    assert rec.last["method"] == "POST"
    assert rec.last["url"] == "https://places.googleapis.com/v1/places:searchNearby"
    assert rec.body_json() == {
        "locationRestriction": {
            "circle": {"center": {"latitude": 45.5, "longitude": -73.6}, "radius": 1500}
        },
        "maxResultCount": 8,
    }


def test_nearby_uses_max_result_count_not_pagesize():
    rec = ok()
    gmaps.main(["nearby", "--near=45.5,-73.6", "--limit=5"], transport=rec)
    body = rec.body_json()
    assert body["maxResultCount"] == 5
    assert "pageSize" not in body


def test_nearby_type_is_plural_array():
    rec = ok()
    gmaps.main(["nearby", "--near=45.5,-73.6", "--type=restaurant"], transport=rec)
    body = rec.body_json()
    assert body["includedTypes"] == ["restaurant"]
    assert "includedType" not in body


def test_nearby_rank_preference_uppercased():
    rec = ok()
    gmaps.main(["nearby", "--near=45.5,-73.6", "--rank=distance"], transport=rec)
    assert rec.body_json()["rankPreference"] == "DISTANCE"
    rec2 = ok()
    gmaps.main(["nearby", "--near=45.5,-73.6", "--rank=popularity"], transport=rec2)
    assert rec2.body_json()["rankPreference"] == "POPULARITY"


def test_nearby_requires_near():
    rc = gmaps.main(["nearby"], transport=ok())
    assert rc == 2


def test_nearby_detailed_mask():
    rec = ok()
    gmaps.main(["nearby", "--near=45.5,-73.6", "--detailed"], transport=rec)
    assert (
        rec.last["headers"]["X-Goog-FieldMask"] == gmaps.FIELD_MASKS["nearby_detailed"]
    )


# --------------------------------------------------------------------------- #
# place
# --------------------------------------------------------------------------- #


def test_place_request_get_bare_mask():
    rec = Recorder(payload={"id": "ChIJabc"})
    rc = gmaps.main(["place", "ChIJabc"], transport=rec)
    assert rc == 0
    assert rec.last["method"] == "GET"
    assert rec.last["url"] == "https://places.googleapis.com/v1/places/ChIJabc"
    h = rec.last["headers"]
    assert h["X-Goog-Api-Key"] == API_KEY
    assert h["X-Goog-FieldMask"] == gmaps.FIELD_MASKS["place"]
    assert "Content-Type" not in h
    assert rec.last["body"] is None


def test_place_lang_query_param():
    rec = Recorder(payload={"id": "ChIJabc"})
    gmaps.main(["place", "ChIJabc", "--lang=fr"], transport=rec)
    assert (
        rec.last["url"]
        == "https://places.googleapis.com/v1/places/ChIJabc?languageCode=fr"
    )


def test_place_reviews_mask_and_trim(capsys):
    payload = {
        "id": "ChIJabc",
        "displayName": {"text": "Trattoria"},
        "location": {"latitude": 43.0, "longitude": 11.0},
        "editorialSummary": {"text": "Cozy Tuscan spot"},
        "regularOpeningHours": {"weekdayDescriptions": ["Mon: 12-22", "Tue: 12-22"]},
        "currentOpeningHours": {"openNow": False},
        "reviews": [
            {
                "rating": 5,
                "text": {"text": "Superb"},
                "authorAttribution": {"displayName": "Ann"},
                "relativePublishTimeDescription": "a week ago",
            },
            {
                "rating": 4,
                "text": {"text": "Good"},
                "authorAttribution": {"displayName": "Bob"},
            },
            {
                "rating": 5,
                "text": {"text": "Great"},
                "authorAttribution": {"displayName": "Cy"},
            },
            {
                "rating": 3,
                "text": {"text": "Meh"},
                "authorAttribution": {"displayName": "Di"},
            },
        ],
    }
    rec = Recorder(payload=payload)
    gmaps.main(["place", "ChIJabc", "--reviews"], transport=rec)
    assert rec.last["headers"]["X-Goog-FieldMask"] == gmaps.FIELD_MASKS["place_reviews"]
    d = json.loads(capsys.readouterr().out)["data"]
    assert d["name"] == "Trattoria"
    assert d["editorial"] == "Cozy Tuscan spot"
    assert d["hours"] == ["Mon: 12-22", "Tue: 12-22"]
    assert d["open_now"] is False
    assert len(d["reviews"]) == 3  # capped at 3


def test_place_not_found_exit_4(capsys):
    rec = err(404, "Requested entity was not found.")
    rc = gmaps.main(["place", "ChIJbad"], transport=rec)
    assert rc == 4
    e = json.loads(capsys.readouterr().err)["error"]
    assert e["retryable"] is False


# --------------------------------------------------------------------------- #
# geocode / revgeocode
# --------------------------------------------------------------------------- #


def test_geocode_request_url_encoded():
    rec = Recorder(payload={"results": []})
    rc = gmaps.main(["geocode", "Café René, Montréal"], transport=rec)
    assert rc == 0
    assert rec.last["method"] == "GET"
    assert rec.last["url"] == (
        "https://geocode.googleapis.com/v4/geocode/address/"
        "Caf%C3%A9%20Ren%C3%A9%2C%20Montr%C3%A9al"
    )
    assert "X-Goog-FieldMask" not in rec.last["headers"]
    assert rec.last["headers"]["X-Goog-Api-Key"] == API_KEY


def test_geocode_lang_query_param():
    rec = Recorder(payload={"results": []})
    gmaps.main(["geocode", "Rome", "--lang=it"], transport=rec)
    assert rec.last["url"].endswith("/address/Rome?languageCode=it")


def test_geocode_trim_output(capsys):
    payload = {
        "results": [
            {
                "formattedAddress": "Rome, Metropolitan City of Rome, Italy",
                "placeId": "ChIJRome",
                "location": {"latitude": 41.9, "longitude": 12.5},
                "granularity": "GEOMETRIC_CENTER",
            }
        ]
    }
    rec = Recorder(payload=payload)
    gmaps.main(["geocode", "Rome"], transport=rec)
    assert json.loads(capsys.readouterr().out)["data"] == [
        {
            "address": "Rome, Metropolitan City of Rome, Italy",
            "lat": 41.9,
            "lng": 12.5,
            "place_id": "ChIJRome",
            "granularity": "GEOMETRIC_CENTER",
        }
    ]


def test_geocode_zero_results_ok(capsys):
    rec = Recorder(payload={"results": []})
    rc = gmaps.main(["geocode", "asdkjfhaskdjfh"], transport=rec)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["data"] == []


def test_revgeocode_at_flag():
    rec = Recorder(payload={"results": []})
    rc = gmaps.main(["revgeocode", "--at=45.51,-73.57"], transport=rec)
    assert rc == 0
    assert rec.last["method"] == "GET"
    assert (
        rec.last["url"]
        == "https://geocode.googleapis.com/v4/geocode/location/45.51,-73.57"
    )


def test_revgeocode_positional():
    rec = Recorder(payload={"results": []})
    gmaps.main(["revgeocode", "45.51,-73.57"], transport=rec)
    assert (
        rec.last["url"]
        == "https://geocode.googleapis.com/v4/geocode/location/45.51,-73.57"
    )


def test_revgeocode_requires_coords():
    rc = gmaps.main(["revgeocode"], transport=Recorder(payload={"results": []}))
    assert rc == 2


# --------------------------------------------------------------------------- #
# route
# --------------------------------------------------------------------------- #


def test_route_address_waypoints():
    rec = Recorder(payload={"routes": []})
    rc = gmaps.main(["route", "--from=Montreal", "--to=Quebec City"], transport=rec)
    assert rc == 0
    assert rec.last["method"] == "POST"
    assert (
        rec.last["url"] == "https://routes.googleapis.com/directions/v2:computeRoutes"
    )
    body = rec.body_json()
    assert body == {
        "origin": {"address": "Montreal"},
        "destination": {"address": "Quebec City"},
        "travelMode": "DRIVE",
    }
    assert "routingPreference" not in body
    assert rec.last["headers"]["X-Goog-FieldMask"] == gmaps.FIELD_MASKS["route"]
    assert rec.last["headers"]["Content-Type"] == "application/json"


def test_route_never_sets_routing_preference():
    rec = Recorder(payload={"routes": []})
    gmaps.main(
        ["route", "--from=A", "--to=B", "--mode=transit", "--lang=fr"], transport=rec
    )
    assert "routingPreference" not in rec.body_json()


def test_route_latlng_and_placeid_waypoints():
    rec = Recorder(payload={"routes": []})
    gmaps.main(
        ["route", "--from=45.5,-73.6", "--to=placeid:ChIJxyz", "--mode=walk"],
        transport=rec,
    )
    body = rec.body_json()
    assert body["origin"] == {
        "location": {"latLng": {"latitude": 45.5, "longitude": -73.6}}
    }
    assert body["destination"] == {"placeId": "ChIJxyz"}
    assert body["travelMode"] == "WALK"


def test_route_mode_mapping():
    for mode, expected in [
        ("drive", "DRIVE"),
        ("walk", "WALK"),
        ("bicycle", "BICYCLE"),
        ("transit", "TRANSIT"),
    ]:
        rec = Recorder(payload={"routes": []})
        gmaps.main(["route", "--from=A", "--to=B", f"--mode={mode}"], transport=rec)
        assert rec.body_json()["travelMode"] == expected


def test_route_trim_output(capsys):
    payload = {
        "routes": [
            {
                "distanceMeters": 5300,
                "duration": "742s",
                "description": "via Autoroute 15",
            }
        ]
    }
    rec = Recorder(payload=payload)
    gmaps.main(["route", "--from=A", "--to=B"], transport=rec)
    assert json.loads(capsys.readouterr().out)["data"] == [
        {
            "distance_m": 5300,
            "duration_s": 742,
            "summary": "via Autoroute 15",
        }
    ]


def test_route_requires_from_and_to():
    assert (
        gmaps.main(["route", "--from=A"], transport=Recorder(payload={"routes": []}))
        == 2
    )
    assert (
        gmaps.main(["route", "--to=B"], transport=Recorder(payload={"routes": []})) == 2
    )


# --------------------------------------------------------------------------- #
# envelope + exit codes
# --------------------------------------------------------------------------- #


def test_success_envelope(capsys):
    rec = ok(payload={"places": []})
    rc = gmaps.main(["search", "x"], transport=rec)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"ok": True, "data": []}


def test_error_envelope_shape(capsys):
    rec = err(403, "PERMISSION_DENIED")
    rc = gmaps.main(["place", "ChIJabc"], transport=rec)
    assert rc == 3
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert set(payload["error"].keys()) == {"code", "message", "retryable", "hint"}
    assert payload["error"]["retryable"] is False


@pytest.mark.parametrize(
    "status,expected",
    [(401, 3), (403, 3), (400, 4), (404, 4), (429, 5), (500, 5), (502, 5), (503, 5)],
)
def test_http_status_to_exit_code(status, expected):
    rc = gmaps.main(["search", "x"], transport=err(status))
    assert rc == expected


def test_upstream_5xx_is_retryable(capsys):
    rc = gmaps.main(["search", "x"], transport=err(503, "unavailable"))
    assert rc == 5
    assert json.loads(capsys.readouterr().err)["error"]["retryable"] is True


def test_timeout_exit_5():
    rec = Recorder(exc=TimeoutError("slow"))
    assert gmaps.main(["search", "x"], transport=rec) == 5


def test_network_error_exit_5(capsys):
    rec = Recorder(exc=urllib.error.URLError("connection refused"))
    rc = gmaps.main(["search", "x"], transport=rec)
    assert rc == 5
    assert json.loads(capsys.readouterr().err)["error"]["retryable"] is True


def test_url_error_wrapping_timeout_exit_5():
    rec = Recorder(exc=urllib.error.URLError(TimeoutError("timed out")))
    assert gmaps.main(["search", "x"], transport=rec) == 5


@pytest.mark.parametrize(
    "argv",
    [
        [],  # no subcommand
        ["bogus"],  # unknown subcommand
        ["search", "x", "--near=not-a-coord"],  # malformed latlng
        ["search", "x", "--near=200,0"],  # latitude out of range
        ["search", "x", "--near=45,-73", "--radius=99999"],  # radius too big
        ["search", "x", "--limit=99"],  # limit out of range
        ["search", "x", "--limit=0"],  # limit too small
        ["nearby"],  # missing required --near
        ["route", "--from=A"],  # missing --to
        ["search", "x", "--unknown-flag=1"],  # unrecognized flag
    ],
)
def test_usage_errors_exit_2(argv):
    assert gmaps.main(argv, transport=ok()) == 2


def test_usage_error_emits_envelope(capsys):
    gmaps.main(["nearby"], transport=ok())
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage"


# --------------------------------------------------------------------------- #
# key handling
# --------------------------------------------------------------------------- #


def test_missing_key_exit_3(monkeypatch, capsys):
    monkeypatch.delenv("GMAPS_API_KEY", raising=False)
    rc = gmaps.main(["search", "x"], transport=ok())
    assert rc == 3
    assert json.loads(capsys.readouterr().err)["ok"] is False


def test_empty_key_exit_3(monkeypatch):
    monkeypatch.setenv("GMAPS_API_KEY", "")
    assert gmaps.main(["search", "x"], transport=ok()) == 3


def test_key_sent_in_header_only():
    rec = ok()
    gmaps.main(["search", "x"], transport=rec)
    assert rec.last["headers"]["X-Goog-Api-Key"] == API_KEY


def test_key_never_in_success_output(capsys):
    payload = {
        "places": [
            {
                "id": "1",
                "displayName": {"text": "X"},
                "formattedAddress": "a",
                "location": {"latitude": 1, "longitude": 2},
                "types": ["cafe"],
                "googleMapsUri": "u",
            }
        ]
    }
    gmaps.main(["search", "x"], transport=ok(payload=payload))
    cap = capsys.readouterr()
    assert API_KEY not in cap.out
    assert API_KEY not in cap.err


def test_key_redacted_even_if_upstream_echoes_it(capsys):
    # Simulate Google echoing the key in an error message; it must be scrubbed.
    rec = Recorder(
        status=403,
        raw=json.dumps(
            {"error": {"message": f"API key {API_KEY} not authorized"}}
        ).encode("utf-8"),
    )
    gmaps.main(["search", "x"], transport=rec)
    cap = capsys.readouterr()
    assert API_KEY not in cap.err
    assert "***" in cap.err


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "x"],
        ["nearby", "--near=45,-73"],
        ["place", "ChIJabc"],
        ["geocode", "Rome"],
        ["revgeocode", "--at=45,-73"],
        ["route", "--from=A", "--to=B"],
    ],
)
def test_key_absent_from_all_subcommand_output(argv, capsys):
    payloads = {"places": [], "results": [], "routes": []}
    gmaps.main(argv, transport=Recorder(payload=payloads))
    cap = capsys.readouterr()
    assert API_KEY not in cap.out
    assert API_KEY not in cap.err


def test_key_not_leaked_via_argparse_error(capsys):
    # A caller who wrongly passes the key as an unknown flag must not see it echoed.
    gmaps.main(["search", "x", f"--api-key={API_KEY}"], transport=ok())
    assert API_KEY not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# --raw + flag forms + Unicode
# --------------------------------------------------------------------------- #


def test_raw_passthrough(capsys):
    payload = {"places": [{"id": "1", "displayName": {"text": "X"}, "weirdField": 123}]}
    gmaps.main(["search", "x", "--raw"], transport=ok(payload=payload))
    assert json.loads(capsys.readouterr().out) == {"ok": True, "data": payload}


def test_flag_equals_and_space_forms_equivalent():
    rec1 = ok()
    gmaps.main(["search", "x", "--limit=5"], transport=rec1)
    rec2 = ok()
    gmaps.main(["search", "x", "--limit", "5"], transport=rec2)
    assert rec1.body_json()["pageSize"] == 5 == rec2.body_json()["pageSize"]


def test_unicode_query_end_to_end(capsys):
    query = "caffè a Montréal, près d'ici"
    payload = {
        "places": [
            {
                "id": "1",
                "displayName": {"text": "Caffè Italia"},
                "formattedAddress": "6840 Boul St-Laurent",
                "location": {"latitude": 45.5, "longitude": -73.6},
                "types": ["cafe"],
                "googleMapsUri": "u",
            }
        ]
    }
    rc = gmaps.main(["search", query], transport=ok(payload=payload))
    assert rc == 0
    # Sent verbatim in the request body.
    # (recorder body reflects exactly what we serialized)
    out = json.loads(capsys.readouterr().out)
    assert out["data"][0]["name"] == "Caffè Italia"


def test_unicode_query_in_request_body():
    query = "caffè a Montréal"
    rec = ok()
    gmaps.main(["search", query], transport=rec)
    assert rec.body_json()["textQuery"] == query

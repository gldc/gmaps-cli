"""v0.2 subcommands: tz (Time Zone API), weather (Weather API), matrix (Route Matrix).

Same hermetic style as test_gmaps.py — injected transport, exact request
assertions, trim shapes, exit codes, key-redaction.
"""

import json
import urllib.parse

import pytest

import gmaps

API_KEY = "TESTKEY-abc123-super-secret"


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("GMAPS_API_KEY", API_KEY)


class Recorder:
    def __init__(self, status=200, payload=None, raw=None):
        self.calls = []
        self.status = status
        self.payload = payload if payload is not None else {}
        self.raw = raw

    def __call__(self, method, url, headers, body):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        if self.raw is not None:
            return self.status, self.raw
        return self.status, json.dumps(self.payload).encode("utf-8")

    @property
    def last(self):
        return self.calls[-1]

    def body_json(self):
        b = self.last["body"]
        return json.loads(b.decode("utf-8")) if b else None


class Seq:
    """Transport returning a different canned response per call, in order."""

    def __init__(self, responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, method, url, headers, body):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        status, payload = self.responses.pop(0)
        return status, json.dumps(payload).encode("utf-8")


def qs(url):
    parts = urllib.parse.urlparse(url)
    return parts, urllib.parse.parse_qs(parts.query)


def out_json(capsys):
    out = capsys.readouterr()
    return json.loads(out.out), out


TZ_OK = {
    "dstOffset": 3600,
    "rawOffset": -18000,
    "status": "OK",
    "timeZoneId": "America/Toronto",
    "timeZoneName": "Eastern Daylight Time",
}


# --------------------------------------------------------------------------- #
# tz
# --------------------------------------------------------------------------- #


def test_tz_request_url_and_trim(capsys):
    rec = Recorder(payload=TZ_OK)
    code = gmaps.main(["tz", "--at=45.5,-73.6", "--time=1710000000"], transport=rec)
    assert code == 0
    assert rec.last["method"] == "GET"
    parts, q = qs(rec.last["url"])
    assert parts.netloc == "maps.googleapis.com"
    assert parts.path == "/maps/api/timezone/json"
    assert q["location"] == ["45.5,-73.6"]
    assert q["timestamp"] == ["1710000000"]
    assert q["key"] == [API_KEY]
    # legacy web service: key is in the URL, never in a header
    assert "X-Goog-Api-Key" not in rec.last["headers"]
    data, _ = out_json(capsys)
    assert data == {
        "ok": True,
        "data": {
            "timezone_id": "America/Toronto",
            "timezone_name": "Eastern Daylight Time",
            "raw_offset_s": -18000,
            "dst_offset_s": 3600,
            "utc_offset_s": -14400,
        },
    }


def test_tz_defaults_timestamp_to_now(monkeypatch):
    rec = Recorder(payload=TZ_OK)
    monkeypatch.setattr(gmaps.time, "time", lambda: 1710000123.7)
    assert gmaps.main(["tz", "--at=45.5,-73.6"], transport=rec) == 0
    _, q = qs(rec.last["url"])
    assert q["timestamp"] == ["1710000123"]


def test_tz_requires_at():
    assert gmaps.main(["tz"], transport=Recorder(payload=TZ_OK)) == 2


@pytest.mark.parametrize(
    "status,exit_code",
    [
        ("ZERO_RESULTS", 4),
        ("REQUEST_DENIED", 3),
        ("OVER_QUERY_LIMIT", 5),
        ("OVER_DAILY_LIMIT", 5),
        ("INVALID_REQUEST", 2),
        ("UNKNOWN_ERROR", 5),
    ],
)
def test_tz_embedded_status_map(status, exit_code, capsys):
    rec = Recorder(payload={"status": status})
    assert gmaps.main(["tz", "--at=0.0,0.0"], transport=rec) == exit_code
    _, out = out_json_err(capsys)
    assert API_KEY not in out.out + out.err


def out_json_err(capsys):
    out = capsys.readouterr()
    return json.loads(out.err), out


def test_tz_retryable_flag_on_over_limit(capsys):
    rec = Recorder(payload={"status": "OVER_QUERY_LIMIT"})
    gmaps.main(["tz", "--at=1,1"], transport=rec)
    err, _ = out_json_err(capsys)
    assert err["ok"] is False
    assert err["error"]["retryable"] is True


def test_tz_raw(capsys):
    rec = Recorder(payload=TZ_OK)
    assert gmaps.main(["tz", "--at=1,1", "--raw"], transport=rec) == 0
    data, _ = out_json(capsys)
    assert data["data"] == TZ_OK


# --------------------------------------------------------------------------- #
# weather
# --------------------------------------------------------------------------- #

WEATHER_CURRENT = {
    "weatherCondition": {"description": {"text": "Sunny"}, "type": "CLEAR"},
    "temperature": {"degrees": 24.1, "unit": "CELSIUS"},
    "feelsLikeTemperature": {"degrees": 26.0, "unit": "CELSIUS"},
    "relativeHumidity": 40,
    "uvIndex": 6,
    "cloudCover": 10,
    "precipitation": {"probability": {"percent": 5, "type": "RAIN"}},
    "wind": {
        "direction": {"degrees": 300, "cardinal": "NORTH_NORTHWEST"},
        "speed": {"value": 12, "unit": "KILOMETERS_PER_HOUR"},
        "gust": {"value": 20, "unit": "KILOMETERS_PER_HOUR"},
    },
}

GEOCODE_ONE = {
    "results": [
        {
            "placeId": "ChIJgeo",
            "formattedAddress": "Rosemère, QC, Canada",
            "location": {"latitude": 45.6389, "longitude": -73.785},
        }
    ]
}


def test_weather_current_by_at(capsys):
    rec = Recorder(payload=WEATHER_CURRENT)
    assert gmaps.main(["weather", "--at=45.5,-73.6"], transport=rec) == 0
    assert len(rec.calls) == 1
    parts, q = qs(rec.last["url"])
    assert parts.netloc == "weather.googleapis.com"
    assert parts.path == "/v1/currentConditions:lookup"
    assert q["location.latitude"] == ["45.5"]
    assert q["location.longitude"] == ["-73.6"]
    assert q["key"] == [API_KEY]
    data, _ = out_json(capsys)
    d = data["data"]
    assert d["condition"] == "Sunny"
    assert d["temp"] == 24.1
    assert d["feels_like"] == 26.0
    assert d["humidity_pct"] == 40
    assert d["wind_kmh"] == 12
    assert d["wind_dir"] == "NORTH_NORTHWEST"
    assert d["precip_prob_pct"] == 5
    assert d["uv_index"] == 6


def test_weather_days_forecast_url():
    rec = Recorder(payload={"forecastDays": []})
    assert gmaps.main(["weather", "--at=45.5,-73.6", "--days=3"], transport=rec) == 0
    parts, q = qs(rec.last["url"])
    assert parts.path == "/v1/forecast/days:lookup"
    assert q["days"] == ["3"]
    assert q["pageSize"] == ["3"]


def test_weather_hours_forecast_url():
    rec = Recorder(payload={"forecastHours": []})
    assert gmaps.main(["weather", "--at=45.5,-73.6", "--hours=6"], transport=rec) == 0
    parts, q = qs(rec.last["url"])
    assert parts.path == "/v1/forecast/hours:lookup"
    assert q["hours"] == ["6"]
    assert q["pageSize"] == ["6"]


def test_weather_units_and_lang_params():
    rec = Recorder(payload=WEATHER_CURRENT)
    gmaps.main(["weather", "--at=1,1", "--units=imperial", "--lang=fr"], transport=rec)
    _, q = qs(rec.last["url"])
    assert q["unitsSystem"] == ["IMPERIAL"]
    assert q["languageCode"] == ["fr"]


def test_weather_place_positional_geocodes_first():
    seq = Seq([(200, GEOCODE_ONE), (200, WEATHER_CURRENT)])
    assert gmaps.main(["weather", "Rosemère QC"], transport=seq) == 0
    assert len(seq.calls) == 2
    g_parts, _ = qs(seq.calls[0]["url"])
    assert g_parts.netloc == "geocode.googleapis.com"
    assert "Rosem%C3%A8re%20QC" in seq.calls[0]["url"]
    _, q = qs(seq.calls[1]["url"])
    assert q["location.latitude"] == ["45.6389"]
    assert q["location.longitude"] == ["-73.785"]


def test_weather_place_not_found():
    seq = Seq([(200, {"results": []})])
    assert gmaps.main(["weather", "Nowhereville"], transport=seq) == 4


@pytest.mark.parametrize(
    "argv",
    [
        ["weather"],  # no location at all
        ["weather", "--at=1,1", "--days=2", "--hours=2"],  # both forecasts
        ["weather", "--at=1,1", "--days=0"],
        ["weather", "--at=1,1", "--days=11"],
        ["weather", "--at=1,1", "--hours=0"],
        ["weather", "--at=1,1", "--hours=25"],
    ],
)
def test_weather_usage_errors(argv):
    assert gmaps.main(argv, transport=Recorder(payload=WEATHER_CURRENT)) == 2


def test_weather_days_trim(capsys):
    payload = {
        "forecastDays": [
            {
                "displayDate": {"year": 2026, "month": 7, "day": 24},
                "maxTemperature": {"degrees": 28.0, "unit": "CELSIUS"},
                "minTemperature": {"degrees": 17.0, "unit": "CELSIUS"},
                "daytimeForecast": {
                    "weatherCondition": {"description": {"text": "Partly sunny"}},
                    "precipitation": {"probability": {"percent": 20}},
                },
                "nighttimeForecast": {
                    "weatherCondition": {"description": {"text": "Clear"}},
                    "precipitation": {"probability": {"percent": 5}},
                },
            }
        ]
    }
    rec = Recorder(payload=payload)
    assert gmaps.main(["weather", "--at=1,1", "--days=1"], transport=rec) == 0
    data, _ = out_json(capsys)
    assert data["data"] == [
        {
            "date": "2026-07-24",
            "condition_day": "Partly sunny",
            "condition_night": "Clear",
            "max": 28.0,
            "min": 17.0,
            "precip_prob_pct": 20,
        }
    ]


def test_weather_key_never_in_output(capsys):
    rec = Recorder(status=403, raw=b'{"error": {"message": "denied"}}')
    assert gmaps.main(["weather", "--at=1,1"], transport=rec) == 3
    out = capsys.readouterr()
    assert API_KEY not in out.out + out.err


# --------------------------------------------------------------------------- #
# matrix
# --------------------------------------------------------------------------- #

MATRIX_RESP = [
    {
        "originIndex": 1,
        "destinationIndex": 0,
        "status": {},
        "condition": "ROUTE_EXISTS",
        "distanceMeters": 1200,
        "duration": "300s",
    },
    {
        "originIndex": 0,
        "destinationIndex": 0,
        "status": {},
        "condition": "ROUTE_EXISTS",
        "distanceMeters": 4500,
        "duration": "780s",
    },
]


def matrix_rec():
    return Recorder(raw=json.dumps(MATRIX_RESP).encode("utf-8"))


def test_matrix_request_shape():
    rec = matrix_rec()
    code = gmaps.main(
        [
            "matrix",
            "--from=Rosemère QC",
            "--from=45.5,-73.6",
            "--to=placeid:ChIJxyz",
            "--mode=walk",
        ],
        transport=rec,
    )
    assert code == 0
    assert rec.last["method"] == "POST"
    assert (
        rec.last["url"]
        == "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
    )
    assert rec.last["headers"]["X-Goog-Api-Key"] == API_KEY
    assert (
        rec.last["headers"]["X-Goog-FieldMask"]
        == "originIndex,destinationIndex,status,condition,distanceMeters,duration"
    )
    body = rec.body_json()
    assert body["origins"] == [
        {"waypoint": {"address": "Rosemère QC"}},
        {"waypoint": {"location": {"latLng": {"latitude": 45.5, "longitude": -73.6}}}},
    ]
    assert body["destinations"] == [{"waypoint": {"placeId": "ChIJxyz"}}]
    assert body["travelMode"] == "WALK"
    assert "routingPreference" not in body


def test_matrix_trim_sorted_by_indexes(capsys):
    rec = matrix_rec()
    gmaps.main(["matrix", "--from=A", "--from=B", "--to=C"], transport=rec)
    data, _ = out_json(capsys)
    assert data["data"] == [
        {
            "from": "A",
            "to": "C",
            "distance_m": 4500,
            "duration_s": 780,
            "condition": "ROUTE_EXISTS",
        },
        {
            "from": "B",
            "to": "C",
            "distance_m": 1200,
            "duration_s": 300,
            "condition": "ROUTE_EXISTS",
        },
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["matrix", "--to=C"],  # no origins
        ["matrix", "--from=A"],  # no destinations
        ["matrix"] + [f"--from=o{i}" for i in range(11)] + ["--to=C"],  # >10
        ["matrix", "--from=A"] + [f"--to=d{i}" for i in range(11)],  # >10
    ],
)
def test_matrix_usage_errors(argv):
    assert gmaps.main(argv, transport=matrix_rec()) == 2


def test_matrix_default_mode_is_drive():
    rec = matrix_rec()
    gmaps.main(["matrix", "--from=A", "--to=B"], transport=rec)
    assert rec.body_json()["travelMode"] == "DRIVE"


# --------------------------------------------------------------------------- #
# Review fixes (adversarial review of feat/v0.2)
# --------------------------------------------------------------------------- #


def test_weather_hours_cap_is_24():
    # forecast/hours pageSize maxes at 24 and the CLI never paginates
    assert gmaps.WEATHER_MAX_HOURS == 24


def test_matrix_dict_response_is_upstream_error(capsys):
    rec = Recorder(payload={"error": {"message": "boom"}})
    assert gmaps.main(["matrix", "--from=A", "--to=B"], transport=rec) == 5
    err = json.loads(capsys.readouterr().err)
    assert err["ok"] is False
    assert err["error"]["retryable"] is True


def test_tz_non_dict_response_is_upstream_error(capsys):
    rec = Recorder(raw=b"[1,2]")
    assert gmaps.main(["tz", "--at=1,1"], transport=rec) == 5
    json.loads(capsys.readouterr().err)  # a JSON envelope, not a traceback


def test_weather_non_dict_response_is_upstream_error(capsys):
    rec = Recorder(raw=b"[]")
    assert gmaps.main(["weather", "--at=1,1"], transport=rec) == 5
    json.loads(capsys.readouterr().err)


def test_matrix_element_error_status_surfaced(capsys):
    resp = [
        {
            "originIndex": 0,
            "destinationIndex": 0,
            "status": {"code": 3, "message": "Origin cannot be geocoded"},
            "condition": "ROUTE_NOT_FOUND",
        }
    ]
    rec = Recorder(raw=json.dumps(resp).encode("utf-8"))
    assert gmaps.main(["matrix", "--from=A", "--to=B"], transport=rec) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data[0]["error"] == "Origin cannot be geocoded"
    assert data[0]["condition"] == "ROUTE_NOT_FOUND"


def test_weather_current_null_fields_no_crash(capsys):
    rec = Recorder(
        payload={
            "temperature": None,
            "weatherCondition": None,
            "wind": None,
            "precipitation": None,
            "feelsLikeTemperature": None,
        }
    )
    assert gmaps.main(["weather", "--at=1,1"], transport=rec) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_weather_day_null_fields_no_crash():
    rec = Recorder(
        payload={
            "forecastDays": [
                {
                    "displayDate": None,
                    "daytimeForecast": None,
                    "nighttimeForecast": None,
                    "maxTemperature": None,
                    "minTemperature": None,
                }
            ]
        }
    )
    assert gmaps.main(["weather", "--at=1,1", "--days=1"], transport=rec) == 0


URLKEY = "TESTKEY+abc/123=="  # urlencodes to TESTKEY%2Babc%2F123%3D%3D


def test_urlencoded_key_scrubbed_from_error_output(capsys, monkeypatch):
    # The headline v0.2 risk: the key rides the URL for tz/weather. If Google
    # ever echoes request params in an error message, BOTH the raw and the
    # percent-encoded key forms must be scrubbed.
    monkeypatch.setenv("GMAPS_API_KEY", URLKEY)
    enc = urllib.parse.quote_plus(URLKEY)
    raw = json.dumps({"error": {"message": f"bad request for ?key={enc}"}})
    rec = Recorder(status=400, raw=raw.encode("utf-8"))
    assert gmaps.main(["tz", "--at=1,1"], transport=rec) == 4
    out = capsys.readouterr()
    assert URLKEY not in out.out + out.err
    assert enc not in out.out + out.err
    assert "***" in out.err


def test_weather_geocode_result_missing_location_is_not_found():
    seq = Seq([(200, {"results": [{"placeId": "x"}]})])
    assert gmaps.main(["weather", "Rosemère"], transport=seq) == 4

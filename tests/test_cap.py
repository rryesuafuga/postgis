from datetime import date, datetime, timedelta, timezone

from ews import cap

SQUARE = {"type": "MultiPolygon",
          "coordinates": [[[[34.2, 0.9], [34.5, 0.9], [34.5, 1.2], [34.2, 1.2], [34.2, 0.9]]],
                          [[[34.0, 0.0], [34.0001, 0.0], [34.0001, 0.0001], [34.0, 0.0]]]]}


def make_input(**kw) -> cap.AlertInput:
    base = dict(
        identifier="UG-EWS-DEMO.UG3020.20220731T090000",
        district={"name": "Bududa", "pcode": "UG3020", "region": "Eastern", "code_scheme": "OCHA-PCODE",
                  "source": "OCHA COD-AB"},
        exceedance={"end_date": date(2022, 7, 31), "accum_days": 3, "accum_mean_mm": 112.4,
                    "accum_max_mm": 168.0, "p95_mean_mm": 43.0, "clim_mean_mm": 14.0, "ratio_to_p95": 2.61,
                    "frac_area_above_p95": 0.85, "triggered_rules": ["P95_AREA50", "P95_MEAN"]},
        level={"level": "warning", "cap_severity": "Severe", "cap_urgency": "Expected",
               "cap_certainty": "Likely", "cap_response": "Prepare"},
        polygons=cap.cap_polygons(SQUARE),
        data_source="CHIRPS v2.0",
        sent=datetime(2022, 7, 31, 9, 0, tzinfo=cap.EAT),
    )
    base.update(kw)
    return cap.AlertInput(**base)


def test_alert_is_valid_and_carries_lifecycle_fields():
    xml = cap.build(make_input())
    assert cap.validate(xml, xsd=None) == []
    s = cap.summary(xml)
    assert s["status"] == "Exercise" and s["msgType"] == "Alert"
    assert s["sent"] == "2022-07-31T09:00:00+03:00"
    assert s["severity"] == "Severe" and s["urgency"] == "Expected" and s["certainty"] == "Likely"
    assert b"<geocode>" in xml and b"<valueName>OCHA-PCODE</valueName>" in xml


def test_polygons_are_closed_lat_lon_and_drop_slivers():
    polys = cap.cap_polygons(SQUARE)
    assert len(polys) == 1                       # the 1e-8 deg2 sliver is dropped
    pairs = polys[0].split()
    assert pairs[0] == pairs[-1] and len(pairs) >= 4
    lat, lon = map(float, pairs[0].split(","))
    assert 0 < lat < 2 and 34 < lon < 35         # lat,lon order


def test_polygon_vertex_cap():
    import math
    ring = [[34 + 0.3 * math.cos(t / 500 * 2 * math.pi), 1 + 0.3 * math.sin(t / 500 * 2 * math.pi)]
            for t in range(500)]
    ring.append(ring[0])
    polys = cap.cap_polygons({"type": "Polygon", "coordinates": [ring]}, max_points=60)
    assert 4 <= len(polys[0].split()) <= 60


def test_update_requires_references():
    xml = cap.build(make_input(msg_type="Update"))
    assert any("requires <references>" in e for e in cap.validate(xml, xsd=None))
    ok = cap.build(make_input(msg_type="Update", references=[
        ("ews-demo@example.org", "UG-EWS-DEMO.UG3020.20220730T090000", "2022-07-30T09:00:00+03:00")]))
    assert cap.validate(ok, xsd=None) == []


def test_cancel_is_all_clear():
    xml = cap.build(make_input(msg_type="Cancel", references=[
        ("ews-demo@example.org", "UG-EWS-DEMO.UG3020.20220730T090000", "2022-07-30T09:00:00+03:00")]))
    assert cap.validate(xml, xsd=None) == []
    assert b"<responseType>AllClear</responseType>" in xml


def test_validator_catches_common_faults():
    xml = cap.build(make_input()).decode()
    broken = (xml.replace("<status>Exercise</status>", "<status>Real</status>")
                 .replace("2022-07-31T09:00:00+03:00</sent>", "2022-07-31T06:00:00Z</sent>"))
    first_poly = xml.split("<polygon>")[1].split("</polygon>")[0]
    broken = broken.replace(first_poly, " ".join(first_poly.split()[:-1]))     # open the ring
    errors = cap.validate(broken.encode(), xsd=None)
    text = " | ".join(errors)
    assert "<status> 'Real'" in text
    assert "not a CAP dateTime" in text
    assert "not closed" in text


def test_element_order_is_enforced():
    xml = cap.build(make_input()).decode()
    swapped = xml.replace("<urgency>Expected</urgency>\n    <severity>Severe</severity>",
                          "<severity>Severe</severity>\n    <urgency>Expected</urgency>")
    assert any("out of order" in e for e in cap.validate(swapped.encode(), xsd=None))


def test_utc_written_as_minus_zero():
    assert cap.cap_datetime(datetime(2022, 7, 31, 6, 0, tzinfo=timezone.utc)) == "2022-07-31T06:00:00-00:00"
    assert cap.cap_datetime(datetime(2022, 7, 31, 9, 0, 0, 123456, tzinfo=cap.EAT)) == "2022-07-31T09:00:00+03:00"


def test_expires_after_sent():
    xml = cap.build(make_input(expires_hours=48))
    s = cap.summary(xml)
    assert datetime.fromisoformat(s["expires"]) - datetime.fromisoformat(s["sent"]) == timedelta(hours=48)

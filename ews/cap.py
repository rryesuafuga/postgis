"""CAP 1.2 (OASIS Common Alerting Protocol) messages for flagged districts.

Lifecycle: an `Alert` opens an incident for a district; `Update` replaces it (new values,
`references` = every earlier message of the incident); `Cancel` closes it (responseType
AllClear). Messages are validated before they are stored in ews.cap_message and written to
out/cap/<identifier>.xml.

Level -> CAP mapping comes from ews.alert_level:
    watch   severity Moderate, urgency Future,   certainty Possible, responseType Monitor
    warning severity Severe,   urgency Expected, certainty Likely,   responseType Prepare
Rationale: the trigger is *observed* rainfall, but the hazard being warned of (flooding,
landslides) is a consequence that is likely/possible, not observed, so certainty describes the
impact. Status defaults to Exercise; Actual requires a named approver, and synthetic inputs force
status Test. These are demonstration choices for the national EW TWG to confirm.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lxml import etree
from lxml.builder import ElementMaker
from shapely.geometry import MultiPolygon, Polygon, shape

from . import config

CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"
EAT = timezone(timedelta(hours=3), "EAT")
XSD_PATH = config.ROOT / "schemas" / "CAP-v1.2.xsd"

ENUMS = {
    "status": {"Actual", "Exercise", "System", "Test", "Draft"},
    "msgType": {"Alert", "Update", "Cancel", "Ack", "Error"},
    "scope": {"Public", "Restricted", "Private"},
    "category": {"Geo", "Met", "Safety", "Security", "Rescue", "Fire", "Health", "Env",
                 "Transport", "Infra", "CBRNE", "Other"},
    "responseType": {"Shelter", "Evacuate", "Prepare", "Execute", "Avoid", "Monitor", "Assess",
                     "AllClear", "None"},
    "urgency": {"Immediate", "Expected", "Future", "Past", "Unknown"},
    "severity": {"Extreme", "Severe", "Moderate", "Minor", "Unknown"},
    "certainty": {"Observed", "Likely", "Possible", "Unlikely", "Unknown"},
}
ALERT_ORDER = ["identifier", "sender", "sent", "status", "msgType", "source", "scope",
               "restriction", "addresses", "code", "note", "references", "incidents", "info"]
ALERT_REQUIRED = ["identifier", "sender", "sent", "status", "msgType", "scope"]
INFO_ORDER = ["language", "category", "event", "responseType", "urgency", "severity", "certainty",
              "audience", "eventCode", "effective", "onset", "expires", "senderName", "headline",
              "description", "instruction", "web", "contact", "parameter", "resource", "area"]
INFO_REQUIRED = ["category", "event", "urgency", "severity", "certainty"]
AREA_ORDER = ["areaDesc", "polygon", "circle", "geocode", "altitude", "ceiling"]
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
BAD_ID_CHARS = re.compile(r"[\s,<&]")

INSTRUCTIONS = {
    "warning": ("Flooding, flash floods and landslides are likely in low-lying areas, along rivers and "
                "on steep slopes. District disaster management committees: activate contingency plans "
                "and prepare to move people out of high-risk areas. Public: do not cross flooded rivers "
                "or streams, keep away from landslide-prone slopes and follow instructions from local "
                "authorities."),
    "watch": ("Heavy rainfall may cause localised flooding and landslides. Monitor rivers and slopes, "
              "review preparedness plans and stay tuned for updates from the national situation room."),
    "cancel": "The heavy rainfall alert for this area is cancelled. No further action is required.",
}


def cap_datetime(dt: datetime) -> str:
    """CAP 1.2 dateTime: no fractional seconds, explicit offset, UTC written as -00:00."""
    s = dt.replace(microsecond=0).isoformat()
    return s[:-6] + "-00:00" if s.endswith("+00:00") else s


def parse_cap_datetime(s: str) -> datetime:
    return datetime.fromisoformat(s)


# --------------------------------------------------------------------------------------------
# Geometry -> <polygon>
# --------------------------------------------------------------------------------------------
def cap_polygons(geojson: dict, max_points: int = 100, max_polygons: int = 20,
                 min_area_frac: float = 0.002) -> list[str]:
    """Exterior rings as CAP "lat,lon lat,lon ..." strings (closed, >= 4 pairs).

    Each ring is simplified until it has at most `max_points` vertices. Very small parts are
    dropped (the <geocode> still identifies the whole district); at most `max_polygons` kept.
    """
    geom = shape(geojson)
    all_parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    total = sum(p.area for p in all_parts) or 1.0
    parts = sorted((p for p in all_parts if p.area / total >= min_area_frac),
                   key=lambda p: -p.area)[:max_polygons]
    if not parts:
        parts = [max(all_parts, key=lambda p: p.area)]
    out = []
    for p in parts:
        ring = Polygon(p.exterior)
        tol = 0.0
        while len(ring.exterior.coords) > max_points:
            tol = tol * 1.5 if tol else 0.001
            ring = Polygon(p.exterior).simplify(tol, preserve_topology=True)
        coords = list(ring.exterior.coords)
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        if len(coords) < 4:
            continue
        out.append(" ".join(f"{lat:.4f},{lon:.4f}" for lon, lat in coords))
    return out


# --------------------------------------------------------------------------------------------
# Message construction
# --------------------------------------------------------------------------------------------
@dataclass
class AlertInput:
    identifier: str
    district: dict              # name, pcode, region, code_scheme, source
    exceedance: dict            # row from ews.district_exceedance (+ rule descriptions)
    level: dict                 # row from ews.alert_level
    polygons: list[str]
    data_source: str
    status: str = "Exercise"
    msg_type: str = "Alert"
    references: list[tuple[str, str, str]] = field(default_factory=list)   # (sender, id, sent)
    incident: str | None = None
    sent: datetime | None = None
    expires_hours: int = 48
    approved_by: str | None = None
    run_id: int | None = None


def build(a: AlertInput) -> bytes:
    sent = (a.sent or datetime.now(EAT)).astimezone(EAT)
    x = a.exceedance
    name, pcode = a.district["name"], a.district["pcode"]
    end_date = x["end_date"]
    level = a.level["level"]
    cancel = a.msg_type == "Cancel"
    E = ElementMaker(namespace=CAP_NS, nsmap={None: CAP_NS})

    notes = []
    if a.status != "Actual":
        notes.append(f"{a.status.upper()} MESSAGE - not an official warning. Produced by a "
                     "demonstration pipeline; warning authority rests with the designated national "
                     "approving authority.")
    if a.approved_by:
        notes.append(f"Approved by: {a.approved_by}.")
    if "SYNTHETIC" in a.data_source.upper():
        notes.append("Generated from SYNTHETIC test data.")

    alert = E.alert(
        E.identifier(a.identifier),
        E.sender(config.CAP["sender"]),
        E.sent(cap_datetime(sent)),
        E.status(a.status),
        E.msgType(a.msg_type),
        E.source(f"Uganda EWS demo pipeline (run {a.run_id})" if a.run_id else "Uganda EWS demo pipeline"),
        E.scope("Public"),
    )
    if notes:
        alert.append(E.note(" ".join(notes)))
    if a.references:
        alert.append(E.references(" ".join(f"{s},{i},{t}" for s, i, t in a.references)))
    if a.incident:
        alert.append(E.incidents(a.incident))

    days = x["accum_days"]
    if cancel:
        headline = f"Cancelled: heavy rainfall alert for {name}"
        description = (f"The heavy rainfall alert for {name} issued on the basis of {days}-day rainfall "
                       f"to {end_date:%d %b %Y} is cancelled.")
        urgency, severity, certainty, response = "Past", "Minor", "Observed", "AllClear"
        instruction = INSTRUCTIONS["cancel"]
    else:
        ratio = x["ratio_to_p95"] or 0
        headline = (f"Heavy rainfall {level}: {name} - {days}-day total {x['accum_mean_mm']:.0f} mm, "
                    f"{ratio:.1f}x the seasonal 95th percentile")
        description = (
            f"Rainfall in the {days} days to {end_date:%d %b %Y} averaged {x['accum_mean_mm']:.0f} mm over "
            f"{name}, {ratio:.1f} times the district's 1991-2020 95th percentile of {days}-day totals for this "
            f"time of year ({x['p95_mean_mm']:.0f} mm; seasonal mean {x['clim_mean_mm']:.0f} mm). "
            f"{100 * x['frac_area_above_p95']:.0f}% of the district was above its local 95th percentile and the "
            f"wettest location received {x['accum_max_mm']:.0f} mm. Triggers met: "
            + "; ".join(x.get("rule_descriptions") or x["triggered_rules"]) + f". Data: {a.data_source}.")
        urgency, severity, certainty = a.level["cap_urgency"], a.level["cap_severity"], a.level["cap_certainty"]
        response, instruction = a.level["cap_response"], INSTRUCTIONS.get(level, INSTRUCTIONS["watch"])
    if a.status in ("Exercise", "Test", "Draft"):
        headline = f"{a.status.upper()}: {headline}"

    info = E.info(
        E.language("en"),
        E.category("Met"),
        E.event("Heavy rainfall"),
        E.responseType(response),
        E.urgency(urgency),
        E.severity(severity),
        E.certainty(certainty),
        E.eventCode(E.valueName("UG-EWS:trigger"), E.value(",".join(x["triggered_rules"]) or "NONE")),
        E.effective(cap_datetime(sent)),
        E.onset(cap_datetime(sent)),
        E.expires(cap_datetime(sent + timedelta(hours=a.expires_hours))),
        E.senderName(config.CAP["sender_name"]),
        E.headline(headline),
        E.description(description),
        E.instruction(instruction),
        E.web(config.CAP["web"]),
        E.contact(config.CAP["contact"]),
    )
    for k, v in (
        ("ACCUM_DAYS", days),
        ("ACCUM_END_DATE", end_date.isoformat()),
        ("ACCUM_MEAN_MM", f"{x['accum_mean_mm']:.1f}"),
        ("ACCUM_MAX_MM", f"{x['accum_max_mm']:.1f}"),
        ("P95_1991_2020_MM", f"{x['p95_mean_mm']:.1f}"),
        ("RATIO_TO_P95", f"{(x['ratio_to_p95'] or 0):.2f}"),
        ("AREA_FRACTION_ABOVE_P95", f"{x['frac_area_above_p95']:.2f}"),
        ("ALERT_LEVEL", level),
        ("DATA_SOURCE", a.data_source),
    ):
        info.append(E.parameter(E.valueName(k), E.value(str(v))))
    area = E.area(E.areaDesc(f"{name} District, Uganda" if "synthetic" not in name.lower()
                             else f"{name}, Uganda (synthetic test zone)"))
    for poly in a.polygons:
        area.append(E.polygon(poly))
    area.append(E.geocode(E.valueName(a.district["code_scheme"]), E.value(pcode)))
    info.append(area)
    alert.append(info)
    return etree.tostring(alert, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------
def _local(el) -> str:
    return etree.QName(el).localname


def _check_order(parent, order: list[str], where: str, errors: list[str]) -> None:
    pos = -1
    for child in parent:
        if not isinstance(child.tag, str):
            continue
        name = _local(child)
        if name not in order:
            errors.append(f"{where}: unexpected element <{name}>")
            continue
        i = order.index(name)
        if i < pos:
            errors.append(f"{where}: <{name}> out of order (CAP 1.2 sequence: {', '.join(order)})")
        pos = max(pos, i)


def _check_polygon(text: str, where: str, errors: list[str]) -> None:
    pairs = text.split()
    if len(pairs) < 4:
        errors.append(f"{where}: polygon needs at least 4 coordinate pairs, has {len(pairs)}")
        return
    if pairs[0] != pairs[-1]:
        errors.append(f"{where}: polygon is not closed (first pair != last pair)")
    for p in pairs:
        try:
            lat, lon = (float(v) for v in p.split(","))
        except ValueError:
            errors.append(f"{where}: bad coordinate pair '{p}' (expected lat,lon)")
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            errors.append(f"{where}: coordinate out of range '{p}'")
            return


def validate(xml: bytes, xsd: Path | None = XSD_PATH) -> list[str]:
    """Return a list of problems (empty = valid). Applies CAP 1.2 rules; also the official
    OASIS XSD when schemas/CAP-v1.2.xsd is present (`make cap-xsd` downloads it)."""
    errors: list[str] = []
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as e:
        return [f"not well-formed XML: {e}"]

    if xsd is not None and Path(xsd).exists():
        schema = etree.XMLSchema(etree.parse(str(xsd)))
        if not schema.validate(root):
            errors += [f"XSD: {e.message} (line {e.line})" for e in schema.error_log]

    if root.tag != f"{{{CAP_NS}}}alert":
        return errors + [f"root element must be {{{CAP_NS}}}alert, found {root.tag}"]
    ns = {"c": CAP_NS}

    def one(parent, name):
        return parent.find(f"c:{name}", ns)

    _check_order(root, ALERT_ORDER, "alert", errors)
    for name in ALERT_REQUIRED:
        el = one(root, name)
        if el is None or not (el.text or "").strip():
            errors.append(f"alert: missing required <{name}>")
    for name in ("status", "msgType", "scope"):
        el = one(root, name)
        if el is not None and el.text not in ENUMS[name]:
            errors.append(f"alert: <{name}> '{el.text}' not in {sorted(ENUMS[name])}")
    for name in ("identifier", "sender"):
        el = one(root, name)
        if el is not None and BAD_ID_CHARS.search(el.text or ""):
            errors.append(f"alert: <{name}> must not contain spaces, commas, '<' or '&'")
    sent = one(root, "sent")
    if sent is not None and not DATETIME_RE.match(sent.text or ""):
        errors.append(f"alert: <sent> '{sent.text}' is not a CAP dateTime (YYYY-MM-DDThh:mm:ss+hh:mm)")
    elif sent is not None and (sent.text or "").endswith("+00:00"):
        errors.append("alert: UTC must be written as -00:00 in CAP 1.2")

    scope = one(root, "scope")
    if scope is not None and scope.text == "Restricted" and one(root, "restriction") is None:
        errors.append("alert: scope Restricted requires <restriction>")
    if scope is not None and scope.text == "Private" and one(root, "addresses") is None:
        errors.append("alert: scope Private requires <addresses>")

    msg_type = one(root, "msgType")
    refs = one(root, "references")
    if msg_type is not None and msg_type.text in ("Update", "Cancel", "Ack", "Error"):
        if refs is None or not (refs.text or "").strip():
            errors.append(f"alert: msgType {msg_type.text} requires <references>")
    if refs is not None:
        for ref in (refs.text or "").split():
            parts = ref.split(",")
            if len(parts) != 3 or not DATETIME_RE.match(parts[2]):
                errors.append(f"alert: reference '{ref}' is not sender,identifier,sent")

    infos = root.findall("c:info", ns)
    if not infos and (msg_type is None or msg_type.text not in ("Cancel", "Ack", "Error")):
        errors.append("alert: at least one <info> is expected for an Alert/Update")
    for n, info in enumerate(infos, 1):
        where = f"info[{n}]"
        _check_order(info, INFO_ORDER, where, errors)
        for name in INFO_REQUIRED:
            if one(info, name) is None:
                errors.append(f"{where}: missing required <{name}>")
        for name in ("category", "responseType"):
            for el in info.findall(f"c:{name}", ns):
                if el.text not in ENUMS[name]:
                    errors.append(f"{where}: <{name}> '{el.text}' not in CAP enumeration")
        for name in ("urgency", "severity", "certainty"):
            el = one(info, name)
            if el is not None and el.text not in ENUMS[name]:
                errors.append(f"{where}: <{name}> '{el.text}' not in {sorted(ENUMS[name])}")
        times = {}
        for name in ("effective", "onset", "expires"):
            el = one(info, name)
            if el is not None:
                if not DATETIME_RE.match(el.text or ""):
                    errors.append(f"{where}: <{name}> '{el.text}' is not a CAP dateTime")
                else:
                    times[name] = parse_cap_datetime(el.text)
        if sent is not None and DATETIME_RE.match(sent.text or "") and "expires" in times:
            if times["expires"] <= parse_cap_datetime(sent.text):
                errors.append(f"{where}: <expires> must be after <sent>")
        lang = one(info, "language")
        if lang is not None and not re.match(r"^[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*$", lang.text or ""):
            errors.append(f"{where}: <language> '{lang.text}' is not an RFC 3066 tag")
        for name in ("eventCode", "parameter"):
            for el in info.findall(f"c:{name}", ns):
                if one(el, "valueName") is None or one(el, "value") is None:
                    errors.append(f"{where}: <{name}> needs <valueName> and <value>")
        for m, area in enumerate(info.findall("c:area", ns), 1):
            aw = f"{where}.area[{m}]"
            _check_order(area, AREA_ORDER, aw, errors)
            if one(area, "areaDesc") is None:
                errors.append(f"{aw}: missing required <areaDesc>")
            for poly in area.findall("c:polygon", ns):
                _check_polygon(poly.text or "", aw, errors)
            for geo in area.findall("c:geocode", ns):
                if one(geo, "valueName") is None or one(geo, "value") is None:
                    errors.append(f"{aw}: <geocode> needs <valueName> and <value>")
            if not area.findall("c:polygon", ns) and not area.findall("c:circle", ns) \
                    and not area.findall("c:geocode", ns):
                errors.append(f"{aw}: no polygon, circle or geocode: the area cannot be targeted")
    return errors


def summary(xml: bytes) -> dict:
    root = etree.fromstring(xml)
    ns = {"c": CAP_NS}
    return {name: root.findtext(f"c:{name}", namespaces=ns) for name in
            ("identifier", "sender", "sent", "status", "msgType", "references", "incidents")} | {
        name: root.findtext(f"c:info/c:{name}", namespaces=ns) for name in
        ("urgency", "severity", "certainty", "expires", "headline", "event")}


def as_json(xml: bytes) -> str:
    return json.dumps(summary(xml), indent=2)

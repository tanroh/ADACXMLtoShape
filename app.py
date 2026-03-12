"""
ADAC XML → ArcGIS Shapefile Converter
Streamlit app — upload one or more ADAC v6 XML files, tune settings, download shapefiles.
"""

import glob
import io
import math
import os
import re as _re
import shutil
import tempfile
import zipfile
from pathlib import Path

import streamlit as st
from lxml import etree
import shapefile  # pyshp

# ─── PAGE CONFIG ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="ADAC → Shapefile Converter",
    page_icon="🗺️",
    layout="wide",
)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

NS  = "http://www.adac.com.au"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ARC_SEGMENTS = 32

FIELD_ABBREV = {
    "AssetID": "AssetID", "Description": "Descript", "InstallDate": "InstDate",
    "AssetOwner": "Owner", "MaintenanceZone": "MaintZone",
    "SurfaceLevel_m": "SurfLvl_m", "InvertLevel_m": "InvLvl_m",
    "US_InvertLevel_m": "US_InvLvl", "DS_InvertLevel_m": "DS_InvLvl",
    "US_SurfaceLevel_m": "US_SrfLvl", "DS_SurfaceLevel_m": "DS_SrfLvl",
    "ShaftSizeDiameter_mm": "ShaftDia", "FloorConstruction": "FloorConst",
    "FloorMaterial": "FloorMat", "WallConstruction": "WallConst",
    "WallMaterial": "WallMat", "RoofMaterial": "RoofMat",
    "LidType": "LidType", "LidAccessRestraint": "LidAccess",
    "LidMaterial": "LidMat", "DropType": "DropType", "Benching": "Benching",
    "CatchmentPS": "CatchPS", "LineNumber": "LineNo", "MH_Number": "MH_Number",
    "Chainage_m": "Chainage", "TieDistance_m": "TieDist",
    "OffsetDistance_m": "OffsetDist", "Rotation": "Rotation",
    "Diameter_mm": "Diam_mm", "RiserDiameter_mm": "RiserDia",
    "InternalDiameter_mm": "IntDia_mm", "BaseMaterial": "BaseMat",
    "RiserMaterial": "RiserMat", "Manufacturer": "Mfr", "ModelNumber": "ModelNo",
    "Material": "Material", "Class": "Class", "Lining": "Lining",
    "Protection": "Protectn", "JointType": "JointType",
    "Alignment_m": "Alignmt", "Depth_m": "Depth_m", "Embedment": "Embedment",
    "RockExcavated": "RockExcav", "PipeGrade": "PipeGrade",
    "Length_m": "Length_m", "GearboxActuator": "GearboxAct",
    "ConnectionType": "ConnType", "BodySize_mm": "BodySz_mm",
    "BranchSize_mm": "BranchSz", "IO_Distance_m": "IO_Dist",
    "SO_Nearest_m": "SO_Near", "SO_Other_m": "SO_Other",
    "Sediment_Trap": "SedTrap", "Offset_m": "Offset_m", "DSMHID": "DSMHID",
    "Volume_m3": "Volume_m3", "Source": "Source", "Use": "Use", "Type": "Type",
    "PitNumber": "PitNumber", "StructureID": "StructID",
    "FittingType": "FittingTyp", "ConcreteCoverType": "ConcCover",
    "FireRetardant": "FireRetard", "CrestElevation_m": "CrestElev",
    "DesignFlow_m3s": "DesignFlow", "HasSpillway": "HasSpillwy",
    "MaintenanceCycle_mnths": "MaintCycle", "TreatmentMeasure": "TreatMeas",
    "DrainShape": "DrainShape", "LiningMaterial": "LiningMat",
    "AverageGrade": "AvgGrade", "LinedWidth_m": "LinedWdt",
    "BaseWidth_m": "BaseWdt", "BatterMaterial": "BatterMat",
    "BatterWidth_m": "BatterWdt", "OutletType": "OutletType",
}

# ─── NAMESPACE HELPERS ────────────────────────────────────────────────────────

def _q(tag):
    return f"{{{NS}}}{tag}"

def _local(elem):
    return etree.QName(elem.tag).localname

def _is_nil(elem):
    return elem.get(f"{{{XSI}}}nil", "false").lower() == "true"

# ─── GEOMETRY PARSING ─────────────────────────────────────────────────────────

def _read_position(elem):
    x_el = elem.find(_q("X"))
    y_el = elem.find(_q("Y"))
    if x_el is None or y_el is None or x_el.text is None or y_el.text is None:
        raise ValueError(f"Missing X/Y in <{_local(elem)}>")
    return (float(x_el.text), float(y_el.text))


def _linearise_arc(from_pt, to_pt, centre_pt, clockwise, n=ARC_SEGMENTS):
    cx, cy = centre_pt
    r  = math.hypot(from_pt[0] - cx, from_pt[1] - cy)
    a0 = math.atan2(from_pt[1] - cy, from_pt[0] - cx)
    a1 = math.atan2(to_pt[1]   - cy, to_pt[0]   - cx)
    if clockwise:
        if a1 >= a0: a1 -= 2 * math.pi
    else:
        if a1 <= a0: a1 += 2 * math.pi
    sweep = a1 - a0
    return [(cx + r * math.cos(a0 + sweep * i / n),
             cy + r * math.sin(a0 + sweep * i / n))
            for i in range(n)]


def _linearise_ellipse(from_pt, to_pt, centre_pt, rotation_deg,
                       semi_major, semi_minor, clockwise, n=ARC_SEGMENTS):
    cx, cy = centre_pt
    rot = math.radians(-rotation_deg)
    cos_r, sin_r = math.cos(rot), math.sin(rot)

    def to_ellipse_angle(pt):
        dx = pt[0] - cx; dy = pt[1] - cy
        lx =  dx * cos_r + dy * sin_r
        ly = -dx * sin_r + dy * cos_r
        return math.atan2(ly / semi_minor, lx / semi_major)

    def from_ellipse_angle(t):
        lx = semi_major * math.cos(t)
        ly = semi_minor * math.sin(t)
        return (cx + lx * cos_r - ly * sin_r, cy + lx * sin_r + ly * cos_r)

    a0 = to_ellipse_angle(from_pt)
    a1 = to_ellipse_angle(to_pt)
    if clockwise:
        if a1 >= a0: a1 -= 2 * math.pi
    else:
        if a1 <= a0: a1 += 2 * math.pi
    sweep = a1 - a0
    return [from_ellipse_angle(a0 + sweep * i / n) for i in range(n)]


def _parse_path_fragments(path_elem, log):
    pts = []
    for child in path_elem:
        tag = _local(child)
        if tag == "PolySegment":
            for vertex in child.findall(_q("Vertex")):
                try:
                    pts.append(_read_position(vertex))
                except ValueError as e:
                    log.append(f"    [WARN] Vertex skipped: {e}")
        elif tag == "CurveCircular":
            try:
                fp = _read_position(child.find(_q("FromPoint")))
                tp = _read_position(child.find(_q("ToPoint")))
                cp = _read_position(child.find(_q("CentrePoint")))
                cw_el = child.find(_q("Clockwise"))
                cw = cw_el is not None and cw_el.text.strip().lower() == "true"
                pts.extend(_linearise_arc(fp, tp, cp, cw))
                pts.append(tp)
            except (ValueError, AttributeError, TypeError) as e:
                log.append(f"    [WARN] CurveCircular skipped: {e}")
        elif tag == "CurveElliptical":
            try:
                fp  = _read_position(child.find(_q("FromPoint")))
                tp  = _read_position(child.find(_q("ToPoint")))
                cp  = _read_position(child.find(_q("CentrePoint")))
                rot = float(child.find(_q("Rotation")).text)
                sm  = float(child.find(_q("SemiMajor")).text)
                sn  = float(child.find(_q("SemiMinor")).text)
                cw_el = child.find(_q("Clockwise"))
                cw = cw_el is not None and cw_el.text.strip().lower() == "true"
                pts.extend(_linearise_ellipse(fp, tp, cp, rot, sm, sn, cw))
                pts.append(tp)
            except (ValueError, AttributeError, TypeError) as e:
                log.append(f"    [WARN] CurveElliptical skipped: {e}")
    return pts


def parse_point_geometry(geom_elem, log):
    if geom_elem is None:
        return None
    pt = geom_elem.find(_q("Point"))
    if pt is not None:
        try: return _read_position(pt)
        except ValueError as e: log.append(f"    [WARN] {e}")
    mp = geom_elem.find(_q("MultiPoint"))
    if mp is not None:
        first = mp.find(_q("Point"))
        if first is not None:
            try: return _read_position(first)
            except ValueError as e: log.append(f"    [WARN] {e}")
    return None


def parse_linear_geometry(geom_elem, log):
    if geom_elem is None:
        return None
    polyline = geom_elem.find(_q("Polyline"))
    if polyline is None:
        return None
    all_pts = []
    for path in polyline.findall(_q("Path")):
        all_pts.extend(_parse_path_fragments(path, log))
    return all_pts if len(all_pts) >= 2 else None


def parse_polygon_geometry(geom_elem, log):
    if geom_elem is None:
        return None
    polygon = geom_elem.find(_q("Polygon"))
    if polygon is None:
        return None
    rings = []
    for ring in polygon.findall(_q("Ring")):
        pts = _parse_path_fragments(ring, log)
        if len(pts) >= 3:
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            rings.append(pts)
    return rings if rings else None


def detect_geometry_kind(geom_elem):
    if geom_elem is None:
        return None
    for child in geom_elem:
        tag = _local(child)
        if tag in ("Point", "MultiPoint"): return "point"
        if tag == "Polyline":              return "linear"
        if tag == "Polygon":               return "polygon"
    return None

# ─── ATTRIBUTE EXTRACTION ─────────────────────────────────────────────────────

_ATTR_SKIP_TOP = {"Geometry"}

def abbrev(name):
    return FIELD_ABBREV.get(name, name[:10])

def extract_all_leaf_fields(elem, prefix=""):
    fields = {}
    for child in elem:
        local = _local(child)
        if local in _ATTR_SKIP_TOP and not prefix:
            continue
        key = f"{prefix}{local}" if prefix else local
        if _is_nil(child):
            fields[abbrev(key)] = None
        elif len(child) == 0:
            fields[abbrev(key)] = child.text
        else:
            fields.update(extract_all_leaf_fields(child, prefix=f"{key}_"))
    return fields

# ─── FEATURE CLASS DISCOVERY ──────────────────────────────────────────────────

def _feature_has_geometry(elem):
    return elem.find(_q("Geometry")) is not None

def discover_feature_classes(root):
    found, seen = [], set()
    for container in root.iter(etree.Element):
        if not isinstance(container.tag, str):
            continue  # skip comments, PIs
        c_local = _local(container)
        if c_local in seen:
            continue
        children = list(container)
        if not children:
            continue
        if not all(_feature_has_geometry(c) for c in children):
            continue
        child_tags = [_local(c) for c in children]
        unique_tags = set(child_tags)
        sample_tag = next(iter(unique_tags))
        if sample_tag == c_local:
            continue
        found.append({
            "container":   c_local,
            "features":    children,
            "homogeneous": len(unique_tags) == 1,
        })
        seen.add(c_local)
    return found

# ─── FEATURE EXTRACTION ───────────────────────────────────────────────────────

def extract_feature_class(fc_info, log, geom_filter):
    features   = fc_info["features"]
    buckets    = {}
    field_sets = {}
    skipped    = 0

    for feat in features:
        geom_elem = feat.find(_q("Geometry"))
        kind      = detect_geometry_kind(geom_elem)

        if kind not in geom_filter:
            skipped += 1
            continue

        if kind == "point":
            geom = parse_point_geometry(geom_elem, log)
        elif kind == "linear":
            geom = parse_linear_geometry(geom_elem, log)
        elif kind == "polygon":
            geom = parse_polygon_geometry(geom_elem, log)
        else:
            skipped += 1
            continue

        if geom is None:
            skipped += 1
            continue

        attrs = extract_all_leaf_fields(feat)
        buckets.setdefault(kind, []).append((geom, attrs))
        if kind not in field_sets:
            field_sets[kind] = {}
        for f in attrs:
            field_sets[kind][f] = None

    if skipped:
        log.append(f"    [INFO] {skipped} feature(s) skipped")

    return {k: (list(field_sets[k].keys()), buckets[k]) for k in buckets}

# ─── PROJECTION STRINGS ───────────────────────────────────────────────────────

PRJ_WGS84 = (
    'GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)

def PRJ_MGA(zone):
    cm = (zone - 30) * 6 - 3
    return (
        f'PROJCS["GDA2020_MGA_Zone_{zone}",'
        'GEOGCS["GCS_GDA2020",DATUM["D_GDA2020",'
        'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Transverse_Mercator"],'
        f'PARAMETER["Central_Meridian",{cm}.0],'
        'PARAMETER["Scale_Factor",0.9996],'
        'PARAMETER["Latitude_Of_Origin",0.0],'
        'PARAMETER["False_Easting",500000.0],'
        'PARAMETER["False_Northing",10000000.0],'
        'UNIT["Meter",1.0]]'
    )

def PRJ_MGA94(zone):
    cm = (zone - 30) * 6 - 3
    return (
        f'PROJCS["GDA94_MGA_Zone_{zone}",'
        'GEOGCS["GCS_GDA_1994",DATUM["D_GDA_1994",'
        'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Transverse_Mercator"],'
        f'PARAMETER["Central_Meridian",{cm}.0],'
        'PARAMETER["Scale_Factor",0.9996],'
        'PARAMETER["Latitude_Of_Origin",0.0],'
        'PARAMETER["False_Easting",500000.0],'
        'PARAMETER["False_Northing",10000000.0],'
        'UNIT["Meter",1.0]]'
    )

# ─── CRS RESOLUTION ───────────────────────────────────────────────────────────

def read_coordinate_system(root):
    cs = root.find(f".//{_q('CoordinateSystem')}")
    if cs is None:
        return None
    return {_local(child): child.text for child in cs}

def _extract_mga_zone(hcs_string):
    if not hcs_string:
        return None
    m = _re.search(r'(?<![0-9])(5[4-8])(?![0-9])', hcs_string)
    return int(m.group(1)) if m else None

def _is_gda94(datum_string):
    if not datum_string:
        return False
    d = datum_string.upper()
    return "94" in d or "1994" in d

def prj_from_adac_crs(crs, fallback=None):
    if crs is None:
        return (fallback or PRJ_WGS84), "fallback (no CoordinateSystem in XML)"
    hcs   = crs.get("HorizontalCoordinateSystem", "")
    datum = crs.get("HorizontalDatum", "")
    zone  = _extract_mga_zone(hcs)
    if zone is None:
        return (fallback or PRJ_WGS84), f"fallback (could not parse zone from '{hcs}')"
    if _is_gda94(datum):
        return PRJ_MGA94(zone), f"GDA94 MGA Zone {zone} (parsed from XML)"
    else:
        return PRJ_MGA(zone),   f"GDA2020 MGA Zone {zone} (parsed from XML)"

def resolve_prj(crs, override_mode, override_datum, override_zone):
    """
    Apply the user's CRS override setting.
    override_mode: 'auto' | 'override'
    """
    if override_mode == "auto":
        return prj_from_adac_crs(crs)
    else:
        zone = override_zone
        if override_datum == "GDA2020":
            return PRJ_MGA(zone), f"GDA2020 MGA Zone {zone} (user override)"
        elif override_datum == "GDA94":
            return PRJ_MGA94(zone), f"GDA94 MGA Zone {zone} (user override)"
        else:
            return PRJ_WGS84, "WGS84 (user override)"

# ─── SHAPEFILE WRITER ─────────────────────────────────────────────────────────

def write_shapefile(output_stem, field_names, records, geom_kind, prj, log):
    shape_type = {"point": shapefile.POINT,
                  "linear": shapefile.POLYLINE,
                  "polygon": shapefile.POLYGON}[geom_kind]
    w = shapefile.Writer(output_stem, shapeType=shape_type)
    for f in field_names:
        w.field(f, "C", 100)
    written = 0
    for geom, attrs in records:
        try:
            if geom_kind == "point":
                w.point(*geom)
            elif geom_kind == "linear":
                w.line([geom])
            else:
                w.poly(geom)
            w.record(*[str(attrs.get(f) or "") for f in field_names])
            written += 1
        except Exception as e:
            log.append(f"    [WARN] Record skipped: {e}")
    w.close()
    with open(f"{output_stem}.prj", "w") as f:
        f.write(prj)
    return written

# ─── MAIN CONVERSION PIPELINE ─────────────────────────────────────────────────

def convert_adac_xml(xml_bytes, filename, output_dir, prj_override_mode,
                     prj_override_datum, prj_override_zone, geom_filter):
    """
    Convert one ADAC XML (as bytes) → shapefiles in output_dir/stem/.
    Returns (summary_list, log_lines, crs_info_dict).
    """
    log = []
    stem = Path(filename).stem

    try:
        root = etree.fromstring(xml_bytes)
    except Exception as e:
        return [], [f"[ERROR] XML parse failed: {e}"], {}

    crs = read_coordinate_system(root)
    crs_info = {}
    if crs:
        crs_info = {
            "HCS": crs.get("HorizontalCoordinateSystem", "?"),
            "Datum": crs.get("HorizontalDatum", "?"),
            "VDatum": crs.get("VerticalDatum", "?"),
            "Approx": crs.get("IsApproximate", "false").lower() == "true",
        }
        log.append(f"CRS (XML): {crs_info['HCS']} / {crs_info['Datum']} "
                   f"(vertical: {crs_info['VDatum']})")
        if crs_info["Approx"]:
            log.append("  [NOTE] CoordinateSystem marked as approximate.")
    else:
        log.append("  [NOTE] No <CoordinateSystem> element found.")

    prj, prj_desc = resolve_prj(crs, prj_override_mode, prj_override_datum, prj_override_zone)
    log.append(f"PRJ used: {prj_desc}")
    crs_info["prj_desc"] = prj_desc

    out_dir = Path(output_dir) / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_classes = discover_feature_classes(root)
    if not feature_classes:
        log.append("  [WARN] No ADAC feature classes discovered.")
        return [], log, crs_info

    log.append(f"Discovered {len(feature_classes)} container(s): "
               f"{[fc['container'] for fc in feature_classes]}")

    summary = []
    for fc in feature_classes:
        container_name = fc["container"]
        n_feats = len(fc["features"])
        log.append(f"\n  ► {container_name} ({n_feats} feature(s), "
                   f"{'homogeneous' if fc['homogeneous'] else 'mixed types'})")

        kind_results = extract_feature_class(fc, log, geom_filter)

        for kind, (field_names, records) in kind_results.items():
            if not records:
                log.append(f"    No {kind} features — skipping.")
                continue

            suffix = f"_{kind}" if not fc["homogeneous"] else ""
            out_stem = str(out_dir / f"{container_name}{suffix}")
            count = write_shapefile(out_stem, field_names, records, kind, prj, log)
            log.append(f"    [{kind}] {count} features → {container_name}{suffix}.shp")
            summary.append({
                "file": filename,
                "layer": f"{container_name}{suffix}",
                "kind": kind,
                "count": count,
                "path": out_stem,
            })

    return summary, log, crs_info


def zip_output_dir(output_dir):
    """Zip the entire output directory and return bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in Path(output_dir).rglob("*"):
            if fpath.is_file():
                zf.write(fpath, fpath.relative_to(output_dir))
    buf.seek(0)
    return buf.read()

# ─── STREAMLIT UI ─────────────────────────────────────────────────────────────

st.title("🗺️ ADAC XML → ArcGIS Shapefile Converter")
st.caption("Converts ADAC v6 XML files to ArcGIS-compatible shapefiles, one per feature class.")

# ── Sidebar settings ──────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")

    st.subheader("Coordinate Reference System")
    crs_mode = st.radio(
        "CRS source",
        options=["Auto-detect from XML", "Override"],
        help="Auto-detect reads the CRS from each XML file. Override forces a specific projection.",
    )

    override_datum = "GDA2020"
    override_zone  = 55

    if crs_mode == "Override":
        override_datum = st.selectbox(
            "Datum",
            ["GDA2020", "GDA94", "WGS84"],
            index=0,
        )
        if override_datum != "WGS84":
            override_zone = st.selectbox(
                "MGA Zone",
                [49, 50, 51, 52, 53, 54, 55, 56, 57, 58],
                index=5,  # 55 default
                help="Common Australian zones: 54 (W QLD), 55 (NSW/VIC/SA), 56 (E QLD/TAS)",
            )

    st.divider()

    st.subheader("Geometry types to export")
    export_points   = st.checkbox("Points",   value=True)
    export_lines    = st.checkbox("Lines",    value=True)
    export_polygons = st.checkbox("Polygons", value=True)

    st.divider()
    st.subheader("Arc linearisation")
    arc_segs = st.slider(
        "Segments per arc",
        min_value=8, max_value=128, value=32, step=8,
        help="Number of straight segments used to approximate circular/elliptical arcs.",
    )

# Apply arc segment setting globally
ARC_SEGMENTS = arc_segs

geom_filter = set()
if export_points:   geom_filter.add("point")
if export_lines:    geom_filter.add("linear")
if export_polygons: geom_filter.add("polygon")

prj_mode = "auto" if crs_mode == "Auto-detect from XML" else "override"

# ── File upload ───────────────────────────────────────────────────────────────
st.subheader("1. Upload ADAC XML file(s)")
uploaded_files = st.file_uploader(
    "Drop one or more ADAC XML files here",
    type=["xml"],
    accept_multiple_files=True,
    label_visibility="collapsed",
)

if not uploaded_files:
    st.info("Upload one or more ADAC XML files to get started.")
    st.stop()

# ── Convert button ────────────────────────────────────────────────────────────
st.subheader("2. Convert")

if not geom_filter:
    st.warning("Please select at least one geometry type to export.")
    st.stop()

col_btn, col_info = st.columns([1, 3])
with col_btn:
    run = st.button("▶ Convert", type="primary", use_container_width=True)

if not run:
    st.stop()

# ── Run conversion ────────────────────────────────────────────────────────────
tmpdir = tempfile.mkdtemp()
all_summary = []
all_logs    = {}
all_crs     = {}

progress = st.progress(0, text="Starting…")
total = len(uploaded_files)

for i, uf in enumerate(uploaded_files):
    progress.progress((i) / total, text=f"Processing {uf.name}…")
    xml_bytes = uf.read()
    summary, log, crs_info = convert_adac_xml(
        xml_bytes, uf.name, tmpdir,
        prj_mode, override_datum, override_zone, geom_filter,
    )
    all_summary.extend(summary)
    all_logs[uf.name]  = log
    all_crs[uf.name]   = crs_info

progress.progress(1.0, text="Done!")

# ── Results ───────────────────────────────────────────────────────────────────
st.subheader("3. Results")

if not all_summary:
    st.error("No features were exported. Check the log below for details.")
else:
    # Summary table
    import pandas as pd
    df = pd.DataFrame(all_summary)[["file", "layer", "kind", "count"]]
    df.columns = ["Source file", "Layer", "Geometry", "Features"]

    # Stats
    c1, c2, c3 = st.columns(3)
    c1.metric("Shapefiles created", len(all_summary))
    c2.metric("Total features", df["Features"].sum())
    c3.metric("Files processed", total)

    st.dataframe(df, use_container_width=True, hide_index=True)

    # Download button
    st.subheader("4. Download")
    zip_bytes = zip_output_dir(tmpdir)
    st.download_button(
        label="⬇️ Download all shapefiles (.zip)",
        data=zip_bytes,
        file_name="adac_shapefiles.zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )

# ── CRS info ─────────────────────────────────────────────────────────────────
with st.expander("CRS / projection details", expanded=False):
    for fname, crs_info in all_crs.items():
        st.markdown(f"**{fname}**")
        if crs_info:
            st.markdown(f"- XML CRS: `{crs_info.get('HCS','?')}` / `{crs_info.get('Datum','?')}`")
            st.markdown(f"- Vertical datum: `{crs_info.get('VDatum','?')}`")
            st.markdown(f"- PRJ applied: `{crs_info.get('prj_desc','?')}`")
            if crs_info.get("Approx"):
                st.warning("CoordinateSystem marked as approximate in XML.")
        else:
            st.caption("No CRS info extracted.")

# ── Conversion log ────────────────────────────────────────────────────────────
with st.expander("Conversion log", expanded=False):
    for fname, log in all_logs.items():
        st.markdown(f"**{fname}**")
        st.code("\n".join(log), language=None)

# ── Cleanup ───────────────────────────────────────────────────────────────────
shutil.rmtree(tmpdir, ignore_errors=True)

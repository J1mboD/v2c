import hmac
import html
import math
import os
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import folium
import requests
import streamlit as st
from PIL import Image, ImageOps
from streamlit_folium import st_folium

try:  # optional: sun-position based time-of-day suggestions
    from astral import Observer
    from astral.sun import elevation as sun_elevation
except Exception:  # pragma: no cover
    Observer = None

try:  # optional: "use my device location" button
    from streamlit_geolocation import streamlit_geolocation
except Exception:  # pragma: no cover
    streamlit_geolocation = None

# ---------- Config ----------
DB_PATH = Path("data/photospots.db")
IMG_DIR = Path("data/images")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)

WEATHER = ["Clear", "Partly cloudy", "Overcast", "Rain", "Storm", "Fog / mist",
           "Snow", "Frost", "Golden haze"]
TIME_OF_DAY = ["Pre-dawn", "Sunrise", "Morning", "Midday", "Afternoon",
               "Golden hour", "Sunset", "Blue hour", "Night"]
SEASONS = ["Spring", "Summer", "Autumn", "Winter"]
TIME_HELP = ("Sunrise/Sunset = sun at the horizon. Golden hour = low warm light either side. "
             "Blue hour = twilight after sunset. Pre-dawn = twilight before sunrise.")

STYLES = [
    "Black & white", "Moody / dramatic sky", "Long exposure", "Silhouette",
    "Minimalist", "High contrast", "Infrared", "Astro", "Panorama",
    "Abstract / detail", "Reflections", "Leading lines",
]
NATURAL_FEATURES = [
    "Coast", "Beach", "Cliffs", "Mountains", "Lake", "River", "Waterfall",
    "Forest", "Moorland", "Desert", "Valley", "Farmland", "Wildlife", "Night sky",
]
MAN_MADE_FEATURES = [
    "Urban", "Architecture", "Bridge", "Ruins",
    "Monument / memorial", "Ancient site", "Castle", "Church / cathedral",
    "Historic building", "Lighthouse", "Breakwater", "Pier", "Harbour",
    "Railway", "Train station", "Viaduct", "Airport",
    "Industrial site", "Quarry / mine", "Reservoir", "Dam", "Canal",
    "Windmill / wind farm",
]
# Conditions used for the "nobody has shot this in..." nudge on each location
GAP_HINTS = {
    "season": SEASONS,
    "time_of_day": ["Sunrise", "Golden hour", "Sunset", "Blue hour", "Night"],
    "weather": ["Clear", "Overcast", "Rain", "Fog / mist", "Snow"],
}
MAX_IMAGE_PX = 1600
MAX_CARDS = 60
RADIUS_OPTIONS = [1, 5, 15, 30, 50, 100]
KM_PER_MILE = 1.609344

# Map settings (edit DEFAULT_CENTER to suit your audience)
DEFAULT_CENTER = (54.5, -3.0)
DEFAULT_ZOOM = 5
ESRI_SAT = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTR = "Tiles © Esri — Esri, Maxar, Earthstar Geographics, and the GIS User Community"


# ---------- Database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                latitude REAL,
                longitude REAL,
                features TEXT DEFAULT '',   -- stored as |Coast|Cliffs|
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                weather TEXT,
                time_of_day TEXT,
                season TEXT,
                styles TEXT DEFAULT '',     -- stored as |Black & white|Long exposure|
                camera_notes TEXT,
                submitted_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_photos_loc ON photos(location_id);
            """
        )
        # migrate databases created by earlier versions
        cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)")]
        if "styles" not in cols:
            conn.execute("ALTER TABLE photos ADD COLUMN styles TEXT DEFAULT ''")
        conn.commit()


def pack(tags):
    return "|" + "|".join(tags) + "|" if tags else ""


def unpack(s):
    return [t for t in (s or "").split("|") if t]


def get_locations():
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM locations ORDER BY name").fetchall()


def add_location(name, description, lat, lon, features):
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO locations (name, description, latitude, longitude, features) "
            "VALUES (?, ?, ?, ?, ?)",
            (name.strip(), description.strip(), lat, lon, pack(features)),
        )
        return cur.lastrowid


def save_image(file_bytes):
    """Validate, orient, downscale and store as JPEG (this also strips EXIF/GPS)."""
    img = Image.open(BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX))
    filename = f"{uuid.uuid4().hex}.jpg"
    img.save(IMG_DIR / filename, "JPEG", quality=85)
    return filename


def add_photo(location_id, filename, weather, tod, season, styles, notes, by):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO photos (location_id, filename, weather, time_of_day, season, "
            "styles, camera_notes, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (location_id, filename, weather, tod, season, pack(styles),
             notes.strip(), by.strip() or "Anonymous"),
        )


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def search_photos(text, weather, tod, seasons, styles, features, origin=None, radius_km=None):
    sql = (
        "SELECT p.*, l.name, l.description, l.latitude, l.longitude, l.features "
        "FROM photos p JOIN locations l ON l.id = p.location_id WHERE 1=1"
    )
    params = []
    if text:
        sql += " AND (l.name LIKE ? OR l.description LIKE ? OR p.camera_notes LIKE ?)"
        params += [f"%{text}%"] * 3
    for col, values in (("p.weather", weather), ("p.time_of_day", tod), ("p.season", seasons)):
        if values:
            sql += f" AND {col} IN ({','.join('?' * len(values))})"
            params += values
    for s in styles:  # photo must have ALL selected styles
        sql += " AND p.styles LIKE ?"
        params.append(f"%|{s}|%")
    for f in features:  # location must have ALL selected features
        sql += " AND l.features LIKE ?"
        params.append(f"%|{f}|%")
    if origin and radius_km:  # cheap bounding box; exact distance is checked below
        lat0, lon0 = origin
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(math.cos(math.radians(lat0)), 0.01))
        sql += " AND l.latitude BETWEEN ? AND ? AND l.longitude BETWEEN ? AND ?"
        params += [lat0 - dlat, lat0 + dlat, lon0 - dlon, lon0 + dlon]
    sql += " ORDER BY p.created_at DESC LIMIT 2000"
    with closing(db()) as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if origin:
        for r in rows:
            r["distance_km"] = (
                haversine_km(origin[0], origin[1], r["latitude"], r["longitude"])
                if r["latitude"] is not None and r["longitude"] is not None else None
            )
        if radius_km:
            rows = [r for r in rows if r["distance_km"] is not None and r["distance_km"] <= radius_km]
        rows.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0))
    return rows


def photos_for_locations(ids):
    if not ids:
        return {}
    with closing(db()) as conn:
        rows = conn.execute(
            f"SELECT * FROM photos WHERE location_id IN ({','.join('?' * len(ids))})", list(ids)
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["location_id"], []).append(dict(r))
    return out


def _remove_image(filename):
    (IMG_DIR / Path(filename).name).unlink(missing_ok=True)


def delete_photo(photo_id):
    """Remove one photo (database row and image file). Admin use only."""
    with closing(db()) as conn, conn:
        row = conn.execute("SELECT filename FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    _remove_image(row["filename"])
    return True


def delete_location(location_id):
    """Remove a location and all of its photos. Admin use only."""
    with closing(db()) as conn, conn:
        files = [r["filename"] for r in
                 conn.execute("SELECT filename FROM photos WHERE location_id = ?", (location_id,))]
        conn.execute("DELETE FROM photos WHERE location_id = ?", (location_id,))
        conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
    for f in files:
        _remove_image(f)
    return len(files)


def admin_photos(location_id=None, limit=30):
    sql = ("SELECT p.*, l.name FROM photos p JOIN locations l ON l.id = p.location_id")
    params = []
    if location_id:
        sql += " WHERE p.location_id = ?"
        params.append(location_id)
    sql += " ORDER BY p.created_at DESC LIMIT ?"
    params.append(limit)
    with closing(db()) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def location_counts():
    with closing(db()) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT l.id, l.name, COUNT(p.id) AS n FROM locations l "
            "LEFT JOIN photos p ON p.location_id = l.id GROUP BY l.id ORDER BY l.name")]


def admin_password():
    """Read the admin password from Streamlit secrets or an environment variable."""
    pw = None
    try:
        pw = st.secrets.get("ADMIN_PASSWORD")
    except Exception:
        pass
    return pw or os.environ.get("PHOTOSPOTS_ADMIN_PASSWORD") or None


# ---------- Summaries ----------
def fmt_counts(counter):
    return ", ".join(f"{k} ×{v}" for k, v in counter.most_common())


def location_summary(photos):
    n = len(photos)
    styles = Counter(s for p in photos for s in unpack(p["styles"]))
    seasons = Counter(p["season"] for p in photos if p["season"])
    times = Counter(p["time_of_day"] for p in photos if p["time_of_day"])
    weather = Counter(p["weather"] for p in photos if p["weather"])
    gaps = (
        [s for s in GAP_HINTS["season"] if s not in seasons]
        + [t for t in GAP_HINTS["time_of_day"] if t not in times]
        + [w for w in GAP_HINTS["weather"] if w not in weather]
    )
    return {"n": n, "styles": styles, "seasons": seasons, "times": times,
            "weather": weather, "gaps": gaps}


def fmt_dist(km, unit):
    d = km / KM_PER_MILE if unit == "miles" else km
    return f"{d:.1f} {'mi' if unit == 'miles' else 'km'}" if d < 10 else f"{d:.0f} {'mi' if unit == 'miles' else 'km'}"


# ---------- EXIF helpers ----------
def _dms(vals):
    d, m, s = (float(v) for v in vals)
    return d + m / 60 + s / 3600


def read_exif(file_bytes):
    """Pull capture time, UTC offset and GPS from a photo, if present."""
    out = {"dt": None, "offset": None, "lat": None, "lon": None}
    try:
        exif = Image.open(BytesIO(file_bytes)).getexif()
        sub = exif.get_ifd(0x8769)
        raw = sub.get(36867) or sub.get(36868) or exif.get(306)
        if raw:
            out["dt"] = datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
        out["offset"] = sub.get(36880)
        gps = exif.get_ifd(0x8825)
        if gps.get(2) and gps.get(4):
            lat, lon = _dms(gps[2]), _dms(gps[4])
            if str(gps.get(1, "N")).upper().startswith("S"):
                lat = -lat
            if str(gps.get(3, "E")).upper().startswith("W"):
                lon = -lon
            if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
                out["lat"], out["lon"] = lat, lon
    except Exception:
        pass
    return out


def suggest_season(dt, lat):
    north = ["Winter", "Winter", "Spring", "Spring", "Spring", "Summer",
             "Summer", "Summer", "Autumn", "Autumn", "Autumn", "Winter"]
    season = north[dt.month - 1]
    if lat is not None and lat < 0:
        season = {"Winter": "Summer", "Summer": "Winter", "Spring": "Autumn", "Autumn": "Spring"}[season]
    return season


def _parse_offset(s):
    try:
        sign = -1 if str(s).startswith("-") else 1
        hh, mm = str(s).lstrip("+-").split(":")[:2]
        return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
    except Exception:
        return None


def suggest_time_of_day(dt, offset, lat, lon):
    """Returns (label, method). Uses sun position when the photo has GPS + UTC offset."""
    tz = _parse_offset(offset) if offset else None
    if Observer and tz and lat is not None and lon is not None:
        try:
            aware = dt.replace(tzinfo=tz)
            obs = Observer(latitude=lat, longitude=lon)
            e = sun_elevation(obs, aware)
            rising = sun_elevation(obs, aware + timedelta(minutes=10)) > e
            if e < -12:
                return "Night", "sun"
            if e < -4:
                return ("Pre-dawn" if rising else "Blue hour"), "sun"
            if e < 3:
                return ("Sunrise" if rising else "Sunset"), "sun"
            if e < 10:
                return "Golden hour", "sun"
            h = dt.hour
            return ("Morning" if h < 11 else "Midday" if h < 14 else "Afternoon"), "sun"
        except Exception:
            pass
    h = dt.hour
    if h < 5 or h >= 22:
        label = "Night"
    elif h < 11:
        label = "Morning"
    elif h < 14:
        label = "Midday"
    elif h < 19:
        label = "Afternoon"
    else:
        label = "Sunset"
    return label, "clock"


# ---------- Map helpers ----------
def base_map(center=DEFAULT_CENTER, zoom=DEFAULT_ZOOM):
    m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True)
    folium.TileLayer(ESRI_SAT, attr=ESRI_ATTR, name="Satellite", max_zoom=19).add_to(m)
    folium.TileLayer("OpenStreetMap", name="Street map").add_to(m)
    folium.TileLayer(ESRI_LABELS, attr=ESRI_ATTR, name="Place labels",
                     overlay=True, show=True).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m


def geocode(query):
    """Look up a place name via OpenStreetMap Nominatim. Returns (lat, lon) or None."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "PhotoSpots-streamlit-app"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


ss = st.session_state


def show_image(img, **kw):
    """Full-width image that works on both older and newer Streamlit versions."""
    try:
        st.image(img, width="stretch", **kw)
    except Exception:
        st.image(img, use_container_width=True, **kw)


def picker_init(name):
    ss.setdefault(f"{name}_pt", None)
    ss.setdefault(f"{name}_center", DEFAULT_CENTER)
    ss.setdefault(f"{name}_zoom", DEFAULT_ZOOM)
    ss.setdefault(f"{name}_nonce", 0)
    ss.setdefault(f"{name}_lastq", "")


def picker_view(name, pt, zoom):
    ss[f"{name}_center"], ss[f"{name}_zoom"] = pt, zoom
    ss[f"{name}_nonce"] += 1  # new map component so the view actually moves


def map_picker(name, marker_color, marker_icon, existing=(), key_suffix="", height=420):
    """Click-to-pick satellite map with place search. Returns the picked (lat, lon) or None."""
    q = st.text_input("Jump to a place (optional)", key=f"{name}_q{key_suffix}",
                      placeholder="e.g. Peel Castle — press Enter")
    if q and q != ss[f"{name}_lastq"]:
        ss[f"{name}_lastq"] = q
        found = geocode(q)
        if found:
            picker_view(name, found, 14)
        else:
            st.warning("Couldn't find that place — try zooming in on the map manually.")

    m = base_map(ss[f"{name}_center"], ss[f"{name}_zoom"])
    for l in existing:  # existing locations, to help avoid duplicates
        if l["latitude"] and l["longitude"]:
            folium.CircleMarker(
                [l["latitude"], l["longitude"]], radius=5, color="#ffcc00",
                fill=True, fill_opacity=0.9, tooltip=l["name"],
            ).add_to(m)
    fg = folium.FeatureGroup(name="Selected")
    if ss[f"{name}_pt"]:
        fg.add_child(folium.Marker(
            list(ss[f"{name}_pt"]), tooltip="Selected",
            icon=folium.Icon(color=marker_color, icon=marker_icon),
        ))
    out = st_folium(
        m, key=f"{name}_map_{ss[f'{name}_nonce']}", feature_group_to_add=fg,
        height=height, use_container_width=True, returned_objects=["last_clicked"],
    )
    click = out.get("last_clicked") if out else None
    if click:
        new = (round(click["lat"], 6), round(click["lng"], 6))
        if new != ss[f"{name}_pt"]:
            ss[f"{name}_pt"] = new
            st.rerun()
    return ss[f"{name}_pt"]


# ---------- UI ----------
st.set_page_config(page_title="PhotoSpots", page_icon="📷", layout="wide")
init_db()
for _n in ("origin", "submit"):
    picker_init(_n)
ss.setdefault("form_nonce", 0)
ss.setdefault("exif_ident", None)
ss.setdefault("exif_note", None)
ss.setdefault("last_geo", None)
ss.setdefault("is_admin", False)
ss.setdefault("admin_fails", 0)
ss.setdefault("admin_locked_until", 0.0)
ss.setdefault("admin_nonce", 0)

st.title("📷 PhotoSpots")
st.caption("Find photography locations by the conditions you want to shoot in.")

# The admin tab is not rendered for normal visitors. Open the app with ?admin in the URL to reveal it.
admin_visible = ("admin" in st.query_params) or ss.is_admin
_labels = ["🔍 Search", "➕ Submit a photo", "🗺️ Map"] + (["🔧 Admin"] if admin_visible else [])
_tabs = st.tabs(_labels)
tab_search, tab_submit, tab_map = _tabs[:3]
tab_admin = _tabs[3] if admin_visible else None

# ----- Search -----
with tab_search:
    with st.sidebar:
        st.header("Filters")
        text = st.text_input("Keyword", placeholder="e.g. lighthouse, misty")
        f_weather = st.multiselect("Weather", WEATHER)
        f_tod = st.multiselect("Time of day", TIME_OF_DAY, help=TIME_HELP)
        f_season = st.multiselect("Season", SEASONS)
        f_styles = st.multiselect("Photo style", STYLES,
                                  help="Photos must have all the styles you select.")
        f_natural = st.multiselect("Natural features", NATURAL_FEATURES)
        f_manmade = st.multiselect("Man-made features", MAN_MADE_FEATURES)
        st.caption("Within weather, time and season, any selected value matches. "
                   "Features and styles must all match.")
        st.divider()
        st.subheader("Distance")
        unit = st.radio("Unit", ["miles", "km"], horizontal=True)
        radius = st.selectbox(
            "Within", ["Any distance"] + RADIUS_OPTIONS,
            format_func=lambda r: r if isinstance(r, str) else f"{r} {unit}",
        )

    radius_km = None if isinstance(radius, str) else radius * (KM_PER_MILE if unit == "miles" else 1)
    origin = ss["origin_pt"]

    with st.expander("📍 Set your location (for distance and radius filtering)",
                     expanded=bool(radius_km) and origin is None):
        st.caption("Click the map, search for a place, or use your device location. "
                   "Your location is only used for this search and is never stored.")
        if streamlit_geolocation:
            geo = streamlit_geolocation()
            if geo and geo.get("latitude") is not None and geo.get("longitude") is not None:
                gp = (round(geo["latitude"], 6), round(geo["longitude"], 6))
                if gp != ss.last_geo:
                    ss.last_geo = gp
                    ss["origin_pt"] = gp
                    picker_view("origin", gp, 11)
                    st.rerun()
        map_picker("origin", "green", "user", height=380)
        if ss["origin_pt"]:
            c1, c2 = st.columns([3, 1])
            c1.success(f"Your location: {ss['origin_pt'][0]:.4f}, {ss['origin_pt'][1]:.4f}")
            if c2.button("Clear"):
                ss["origin_pt"] = None
                ss["origin_nonce"] += 1
                st.rerun()
    origin = ss["origin_pt"]
    if radius_km and not origin:
        st.info("Set your location above to use the distance filter.")
        radius_km = None

    results = search_photos(
        text, f_weather, f_tod, f_season, f_styles, f_natural + f_manmade,
        origin=origin, radius_km=radius_km,
    )
    view = st.radio("View", ["Photos", "Locations"], horizontal=True)

    if view == "Photos":
        st.subheader(f"{len(results)} photo(s) found")
        if not results:
            st.info("No matches yet. Try fewer filters, or submit the first example!")
        if len(results) > MAX_CARDS:
            st.caption(f"Showing the first {MAX_CARDS}. Add filters to narrow down.")
        cols = st.columns(3)
        for i, r in enumerate(results[:MAX_CARDS]):
            with cols[i % 3]:
                with st.container(border=True):
                    path = IMG_DIR / r["filename"]
                    if path.exists():
                        show_image(str(path))
                    st.markdown(f"**{r['name']}**")
                    if r.get("distance_km") is not None:
                        st.caption(f"📏 {fmt_dist(r['distance_km'], unit)} away")
                    tags = [t for t in (r["weather"], r["time_of_day"], r["season"]) if t]
                    st.caption(" · ".join(tags))
                    if unpack(r["styles"]):
                        st.caption("🎞️ " + ", ".join(unpack(r["styles"])))
                    if unpack(r["features"]):
                        st.caption("🏞️ " + ", ".join(unpack(r["features"])))
                    if r["camera_notes"]:
                        st.write(r["camera_notes"])
                    if r["latitude"] and r["longitude"]:
                        st.markdown(
                            f"[📍 Open in Maps](https://www.google.com/maps?q={r['latitude']},{r['longitude']})"
                        )
                    st.caption(f"By {r['submitted_by']}")
    else:
        groups = {}
        for r in results:
            groups.setdefault(r["location_id"], []).append(r)
        all_photos = photos_for_locations(list(groups))
        st.subheader(f"{len(groups)} location(s) found")
        if not groups:
            st.info("No matches yet. Try fewer filters, or submit the first example!")
        cols = st.columns(3)
        for i, (loc_id, matched) in enumerate(list(groups.items())[:MAX_CARDS]):
            r0 = matched[0]
            summ = location_summary(all_photos.get(loc_id, matched))
            with cols[i % 3]:
                with st.container(border=True):
                    path = IMG_DIR / r0["filename"]
                    if path.exists():
                        show_image(str(path))
                    st.markdown(f"**{r0['name']}**")
                    if r0.get("distance_km") is not None:
                        st.caption(f"📏 {fmt_dist(r0['distance_km'], unit)} away")
                    if r0["description"]:
                        st.write(r0["description"])
                    if unpack(r0["features"]):
                        st.caption("🏞️ " + ", ".join(unpack(r0["features"])))
                    st.caption(f"📷 {summ['n']} photo(s), {len(matched)} matching your filters")
                    if summ["styles"]:
                        st.caption("🎞️ " + fmt_counts(summ["styles"]))
                    if summ["seasons"]:
                        st.caption("🍂 " + fmt_counts(summ["seasons"]))
                    if summ["times"]:
                        st.caption("🕒 " + fmt_counts(summ["times"]))
                    if summ["weather"]:
                        st.caption("🌦️ " + fmt_counts(summ["weather"]))
                    if summ["gaps"]:
                        more = "…" if len(summ["gaps"]) > 8 else ""
                        st.caption("🔭 Nobody has shot this in: " + ", ".join(summ["gaps"][:8]) + more)
                    if len(matched) > 1:
                        with st.expander(f"More matching photos ({len(matched) - 1})"):
                            paths = [str(IMG_DIR / p["filename"]) for p in matched[1:]
                                     if (IMG_DIR / p["filename"]).exists()]
                            if paths:
                                show_image(paths)
                    if r0["latitude"] and r0["longitude"]:
                        st.markdown(
                            f"[📍 Open in Maps](https://www.google.com/maps?q={r0['latitude']},{r0['longitude']})"
                        )

# ----- Submit -----
with tab_submit:
    st.subheader("Submit a sample photo")
    if ss.get("flash"):
        st.success(ss.pop("flash"))
    k = ss.form_nonce  # bumping this resets every widget below after a successful submit

    # 1. Photo (outside any form so we can pre-fill from its EXIF data)
    st.markdown("**1. Your photo**")
    upload = st.file_uploader("Photo *", type=["jpg", "jpeg", "png", "webp"], key=f"upload_{k}")
    if upload:
        ident = (upload.name, upload.size)
        if ss.exif_ident != ident:
            ss.exif_ident = ident
            info = read_exif(upload.getvalue())
            bits = []
            if info["dt"]:
                ss[f"season_{k}"] = suggest_season(info["dt"], info["lat"])
                tod, method = suggest_time_of_day(info["dt"], info["offset"], info["lat"], info["lon"])
                ss[f"tod_{k}"] = tod
                bits.append(
                    f"Pre-filled season ({ss[f'season_{k}']}) and time of day ({tod}) from the photo's "
                    + ("date and the sun's position." if method == "sun"
                       else "capture time (a rough estimate).")
                )
                if info["lat"] is None:
                    bits.append("It has no GPS, so the season assumes the northern hemisphere.")
            if info["lat"] is not None:
                ss["submit_pt"] = (round(info["lat"], 6), round(info["lon"], 6))
                picker_view("submit", ss["submit_pt"], 15)
                bits.append("The photo's GPS position is used as the starting point on the map for a new location.")
            if bits:
                bits.append("Please check these are right; weather can't be read from a photo.")
            ss.exif_note = " ".join(bits) or None
        st.image(upload, width=320)
        if ss.exif_note:
            st.info(ss.exif_note)
        st.caption("Uploaded images are resized and stored without their metadata (including GPS).")
    else:
        ss.exif_ident = None
        ss.exif_note = None

    # 2. Location
    st.markdown("**2. Location**")
    locations = get_locations()
    NEW = "➕ New location"
    choice = st.selectbox("Location", [NEW] + [f"{l['name']} (#{l['id']})" for l in locations],
                          key=f"loc_{k}")
    is_new = choice == NEW
    if is_new:
        picked = map_picker("submit", "red", "camera", existing=locations, key_suffix=f"_{k}")
        if picked:
            st.success(f"Selected: {picked[0]:.5f}, {picked[1]:.5f}  (yellow dots are existing locations)")
        else:
            st.info("Zoom in and click the exact spot to select it.")
        loc_name = st.text_input("Location name *", key=f"name_{k}")
        loc_desc = st.text_area("Description / access tips", key=f"desc_{k}")
        loc_natural = st.multiselect("Natural features", NATURAL_FEATURES, key=f"nat_{k}")
        loc_manmade = st.multiselect("Man-made features", MAN_MADE_FEATURES, key=f"man_{k}")

    # 3. Conditions
    st.markdown("**3. Conditions when the photo was taken**")
    st.caption("Describe what the photo actually shows, not what the place might suit.")
    c1, c2, c3 = st.columns(3)
    weather = c1.selectbox("Weather *", WEATHER, index=None, placeholder="Choose...", key=f"weather_{k}")
    tod = c2.selectbox("Time of day *", TIME_OF_DAY, index=None, placeholder="Choose...",
                       help=TIME_HELP, key=f"tod_{k}")
    season = c3.selectbox("Season *", SEASONS, index=None, placeholder="Choose...", key=f"season_{k}")
    styles = st.multiselect("Photo style (optional)", STYLES, key=f"styles_{k}",
                            help="How you processed or composed this shot, e.g. Black & white.")
    notes = st.text_area("Camera / composition notes", placeholder="Lens, settings, viewpoint...",
                         key=f"notes_{k}")
    by = st.text_input("Your name (optional)", key=f"by_{k}")
    agree = st.checkbox("I took this photo and am happy for it to be shown on this site.",
                        key=f"agree_{k}")

    if st.button("Submit", type="primary", key=f"submit_{k}"):
        errors = []
        if not upload:
            errors.append("Please upload a photo.")
        if not (weather and tod and season):
            errors.append("Please choose the weather, time of day and season.")
        if not agree:
            errors.append("Please confirm you own the photo.")
        if is_new:
            if not loc_name.strip():
                errors.append("Please enter a location name.")
            if not ss["submit_pt"]:
                errors.append("Please click the map to choose the location.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                filename = save_image(upload.getvalue())
            except Exception:
                st.error("That file couldn't be read as an image.")
            else:
                if is_new:
                    loc_id = add_location(
                        loc_name, loc_desc, ss["submit_pt"][0], ss["submit_pt"][1],
                        loc_natural + loc_manmade,
                    )
                else:
                    loc_id = int(choice.rsplit("#", 1)[1].rstrip(")"))
                add_photo(loc_id, filename, weather, tod, season, styles, notes, by)
                ss.form_nonce += 1
                ss["submit_pt"] = None
                ss["submit_nonce"] += 1
                ss["submit_lastq"] = ""
                ss.exif_ident = None
                ss.exif_note = None
                ss.flash = "Thanks! Your photo has been added."
                st.rerun()

# ----- Map -----
with tab_map:
    locs = [l for l in get_locations() if l["latitude"] and l["longitude"]]
    if not locs:
        st.info("No locations with coordinates yet.")
    else:
        m = base_map()
        for l in locs:
            popup = f"<b>{html.escape(l['name'])}</b><br>{html.escape(l['description'] or '')}"
            folium.Marker(
                [l["latitude"], l["longitude"]], tooltip=l["name"],
                popup=folium.Popup(popup, max_width=250),
                icon=folium.Icon(color="blue", icon="camera"),
            ).add_to(m)
        lats = [l["latitude"] for l in locs]
        lons = [l["longitude"] for l in locs]
        m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], max_zoom=12)
        st_folium(m, key="overview", height=550, use_container_width=True, returned_objects=[])

# ----- Admin (hidden) -----
if tab_admin is not None:
    with tab_admin:
        st.subheader("Admin")
        expected = admin_password()
        if not expected:
            st.warning("Admin access isn't configured. Set ADMIN_PASSWORD in "
                       ".streamlit/secrets.toml (or the PHOTOSPOTS_ADMIN_PASSWORD environment variable).")
        elif not ss.is_admin:
            locked = time.time() < ss.admin_locked_until
            pw = st.text_input("Admin password", type="password", key="admin_pw")
            if st.button("Log in", disabled=locked):
                if hmac.compare_digest(pw.encode(), expected.encode()):
                    ss.is_admin = True
                    ss.admin_fails = 0
                    st.rerun()
                else:
                    ss.admin_fails += 1
                    time.sleep(1)  # slow down guessing
                    if ss.admin_fails >= 5:
                        ss.admin_locked_until = time.time() + 60
                        ss.admin_fails = 0
                    st.error("Incorrect password.")
            if locked:
                st.warning("Too many attempts. Please wait a minute and try again.")
        else:
            c1, c2 = st.columns([4, 1])
            c1.success("Logged in as admin")
            if c2.button("Log out"):
                ss.is_admin = False
                ss.pop("confirm_photo", None)
                st.rerun()
            if ss.get("admin_msg"):
                st.success(ss.pop("admin_msg"))

            st.markdown("### Photos")
            counts = location_counts()
            flt = st.selectbox("Filter by location", ["All locations"] +
                               [f"{c['name']} (#{c['id']})" for c in counts], key="admin_filter")
            flt_id = None if flt == "All locations" else int(flt.rsplit("#", 1)[1].rstrip(")"))
            photos = admin_photos(flt_id)
            if not photos:
                st.info("No photos to show.")
            for p in photos:
                with st.container(border=True):
                    c_img, c_info, c_act = st.columns([1, 3, 1])
                    path = IMG_DIR / Path(p["filename"]).name
                    if path.exists():
                        c_img.image(str(path), width=120)
                    c_info.markdown(f"**{p['name']}** · photo #{p['id']}")
                    c_info.caption(f"{p['weather']} · {p['time_of_day']} · {p['season']} · "
                                   f"by {p['submitted_by']} · {p['created_at']}")
                    if ss.get("confirm_photo") == p["id"]:
                        c_act.warning("Delete this photo?")
                        if c_act.button("Yes, delete", key=f"yes_{p['id']}", type="primary"):
                            delete_photo(p["id"])
                            ss.pop("confirm_photo", None)
                            ss.admin_msg = f"Deleted photo #{p['id']}."
                            st.rerun()
                        if c_act.button("Cancel", key=f"no_{p['id']}"):
                            ss.pop("confirm_photo", None)
                            st.rerun()
                    elif c_act.button("Delete", key=f"del_{p['id']}"):
                        ss.confirm_photo = p["id"]
                        st.rerun()

            st.markdown("### Delete a whole location")
            st.caption("Removes the location and every photo submitted to it. "
                       "Locations with 0 photos are hidden from search but still appear on the map.")
            if counts:
                n = ss.admin_nonce
                target = st.selectbox("Location", counts, key=f"admin_loc_{n}",
                                      format_func=lambda c: f"{c['name']} (#{c['id']}), {c['n']} photo(s)")
                ok = st.checkbox(f"I understand this permanently deletes '{target['name']}' "
                                 f"and its {target['n']} photo(s).", key=f"admin_ok_{n}")
                if st.button("Delete location", disabled=not ok, key=f"admin_delloc_{n}"):
                    removed = delete_location(target["id"])
                    ss.admin_nonce += 1
                    ss.admin_msg = f"Deleted '{target['name']}' and {removed} photo(s)."
                    st.rerun()

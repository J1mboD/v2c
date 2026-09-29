import html
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import folium
import requests
import streamlit as st
from PIL import Image, ImageOps
from streamlit_folium import st_folium

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
MAX_IMAGE_PX = 1600

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
    with closing(db()) as conn, conn:
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
                camera_notes TEXT,
                submitted_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_photos_loc ON photos(location_id);
            """
        )


def get_locations():
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM locations ORDER BY name").fetchall()


def add_location(name, description, lat, lon, features):
    feat = "|" + "|".join(features) + "|" if features else ""
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO locations (name, description, latitude, longitude, features) "
            "VALUES (?, ?, ?, ?, ?)",
            (name.strip(), description.strip(), lat, lon, feat),
        )
        return cur.lastrowid


def save_image(uploaded_file):
    """Validate, orient, downscale and store as JPEG. Returns filename."""
    img = Image.open(uploaded_file)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX))
    filename = f"{uuid.uuid4().hex}.jpg"
    img.save(IMG_DIR / filename, "JPEG", quality=85)
    return filename


def add_photo(location_id, filename, weather, tod, season, notes, by):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO photos (location_id, filename, weather, time_of_day, season, "
            "camera_notes, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (location_id, filename, weather, tod, season, notes.strip(), by.strip() or "Anonymous"),
        )


def search_photos(text, weather, tod, seasons, features, limit=60):
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
    for f in features:  # location must have ALL selected features
        sql += " AND l.features LIKE ?"
        params.append(f"%|{f}|%")
    sql += " ORDER BY p.created_at DESC LIMIT ?"
    params.append(limit)
    with closing(db()) as conn:
        return conn.execute(sql, params).fetchall()


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


# ---------- UI ----------
st.set_page_config(page_title="PhotoSpots", page_icon="📷", layout="wide")
init_db()
ss = st.session_state
ss.setdefault("picked", None)
ss.setdefault("map_center", DEFAULT_CENTER)
ss.setdefault("map_zoom", DEFAULT_ZOOM)
ss.setdefault("map_nonce", 0)
ss.setdefault("last_place_q", "")

st.title("📷 PhotoSpots")
st.caption("Find photography locations by the conditions you want to shoot in.")

tab_search, tab_submit, tab_map = st.tabs(["🔍 Search", "➕ Submit a photo", "🗺️ Map"])

# ----- Search -----
with tab_search:
    with st.sidebar:
        st.header("Filters")
        text = st.text_input("Keyword", placeholder="e.g. lighthouse, misty")
        f_weather = st.multiselect("Weather", WEATHER)
        f_tod = st.multiselect("Time of day", TIME_OF_DAY)
        f_season = st.multiselect("Season", SEASONS)
        f_natural = st.multiselect("Natural features", NATURAL_FEATURES)
        f_manmade = st.multiselect("Man-made features", MAN_MADE_FEATURES)
        st.caption("Locations must have all selected features.")

    results = search_photos(text, f_weather, f_tod, f_season, f_natural + f_manmade)
    st.subheader(f"{len(results)} photo(s) found")
    if not results:
        st.info("No matches yet. Try fewer filters, or submit the first example!")

    cols = st.columns(3)
    for i, r in enumerate(results):
        with cols[i % 3]:
            with st.container(border=True):
                path = IMG_DIR / r["filename"]
                if path.exists():
                    st.image(str(path), use_container_width=True)
                st.markdown(f"**{r['name']}**")
                tags = [t for t in (r["weather"], r["time_of_day"], r["season"]) if t]
                st.caption(" · ".join(tags))
                feats = [f for f in (r["features"] or "").split("|") if f]
                if feats:
                    st.caption("🏞️ " + ", ".join(feats))
                if r["camera_notes"]:
                    st.write(r["camera_notes"])
                if r["latitude"] and r["longitude"]:
                    st.markdown(
                        f"[📍 Open in Maps](https://www.google.com/maps?q={r['latitude']},{r['longitude']})"
                    )
                st.caption(f"By {r['submitted_by']}")

# ----- Submit -----
with tab_submit:
    st.subheader("Submit a sample photo")
    if ss.get("flash"):
        st.success(ss.pop("flash"))

    locations = get_locations()
    NEW = "➕ New location"
    choice = st.selectbox(
        "Location", [NEW] + [f"{l['name']} (#{l['id']})" for l in locations]
    )
    is_new = choice == NEW

    if is_new:
        st.markdown("**1. Pick the spot on the map**")
        place_q = st.text_input(
            "Jump to a place (optional)", key="place_q",
            placeholder="e.g. Peel Castle — press Enter",
        )
        if place_q and place_q != ss.last_place_q:
            ss.last_place_q = place_q
            found = geocode(place_q)
            if found:
                ss.map_center, ss.map_zoom = found, 14
                ss.map_nonce += 1
            else:
                st.warning("Couldn't find that place — try zooming in on the map manually.")

        m = base_map(ss.map_center, ss.map_zoom)
        for l in locations:  # existing locations, to help avoid duplicates
            if l["latitude"] and l["longitude"]:
                folium.CircleMarker(
                    [l["latitude"], l["longitude"]], radius=5, color="#ffcc00",
                    fill=True, fill_opacity=0.9, tooltip=l["name"],
                ).add_to(m)
        fg = folium.FeatureGroup(name="Selected")
        if ss.picked:
            fg.add_child(folium.Marker(
                list(ss.picked), tooltip="Selected location",
                icon=folium.Icon(color="red", icon="camera"),
            ))
        out = st_folium(
            m, key=f"picker_{ss.map_nonce}", feature_group_to_add=fg,
            height=450, use_container_width=True, returned_objects=["last_clicked"],
        )
        click = out.get("last_clicked") if out else None
        if click:
            new = (round(click["lat"], 6), round(click["lng"], 6))
            if new != ss.picked:
                ss.picked = new
                st.rerun()

        if ss.picked:
            st.success(f"Selected: {ss.picked[0]:.5f}, {ss.picked[1]:.5f}  (yellow dots are existing locations)")
        else:
            st.info("Zoom in and click the exact spot to select it.")

    with st.form("submit_form", clear_on_submit=True):
        if is_new:
            st.markdown("**2. New location details**")
            loc_name = st.text_input("Location name *")
            loc_desc = st.text_area("Description / access tips")
            loc_natural = st.multiselect("Natural features", NATURAL_FEATURES)
            loc_manmade = st.multiselect("Man-made features", MAN_MADE_FEATURES)

        st.markdown("**Photo details**" if not is_new else "**3. Photo details**")
        upload = st.file_uploader("Photo *", type=["jpg", "jpeg", "png", "webp"])
        c1, c2, c3 = st.columns(3)
        weather = c1.selectbox("Weather", WEATHER)
        tod = c2.selectbox("Time of day", TIME_OF_DAY)
        season = c3.selectbox("Season", SEASONS)
        notes = st.text_area("Camera / composition notes", placeholder="Lens, settings, viewpoint...")
        by = st.text_input("Your name (optional)")
        agree = st.checkbox("I took this photo and am happy for it to be shown on this site.")
        submitted = st.form_submit_button("Submit")

    if submitted:
        errors = []
        if not upload:
            errors.append("Please upload a photo.")
        if not agree:
            errors.append("Please confirm you own the photo.")
        if is_new:
            if not loc_name.strip():
                errors.append("Please enter a location name.")
            if not ss.picked:
                errors.append("Please click the map to choose the location.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                filename = save_image(upload)
            except Exception:
                st.error("That file couldn't be read as an image.")
            else:
                if is_new:
                    loc_id = add_location(
                        loc_name, loc_desc, ss.picked[0], ss.picked[1],
                        loc_natural + loc_manmade,
                    )
                else:
                    loc_id = int(choice.rsplit("#", 1)[1].rstrip(")"))
                add_photo(loc_id, filename, weather, tod, season, notes, by)
                ss.picked = None
                ss.map_nonce += 1  # fresh map component, clears the old click
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

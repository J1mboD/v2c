import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image, ImageOps

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
FEATURES = ["Coast", "Beach", "Cliffs", "Mountains", "Lake", "River", "Waterfall",
            "Forest", "Moorland", "Desert", "Valley", "Urban", "Architecture",
            "Bridge", "Ruins", "Farmland", "Wildlife", "Night sky"]
MAX_IMAGE_PX = 1600


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


# ---------- UI ----------
st.set_page_config(page_title="PhotoSpots", page_icon="📷", layout="wide")
init_db()

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
        f_features = st.multiselect("Geographical features", FEATURES,
                                    help="Locations must have all selected features.")

    results = search_photos(text, f_weather, f_tod, f_season, f_features)
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
    locations = get_locations()
    NEW = "➕ New location"
    choice = st.selectbox(
        "Location", [NEW] + [f"{l['name']} (#{l['id']})" for l in locations]
    )
    is_new = choice == NEW

    with st.form("submit_form", clear_on_submit=True):
        if is_new:
            st.markdown("**New location details**")
            loc_name = st.text_input("Location name *")
            loc_desc = st.text_area("Description / access tips")
            c1, c2 = st.columns(2)
            lat = c1.number_input("Latitude", -90.0, 90.0, 0.0, format="%.5f")
            lon = c2.number_input("Longitude", -180.0, 180.0, 0.0, format="%.5f")
            loc_features = st.multiselect("Geographical features", FEATURES)

        st.markdown("**Photo details**")
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
            if lat == 0.0 and lon == 0.0:
                errors.append("Please enter coordinates for the new location.")
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
                    loc_id = add_location(loc_name, loc_desc, lat, lon, loc_features)
                else:
                    loc_id = int(choice.rsplit("#", 1)[1].rstrip(")"))
                add_photo(loc_id, filename, weather, tod, season, notes, by)
                st.success("Thanks! Your photo has been added.")

# ----- Map -----
with tab_map:
    locs = get_locations()
    df = pd.DataFrame(
        [{"name": l["name"], "latitude": l["latitude"], "longitude": l["longitude"]}
         for l in locs if l["latitude"] and l["longitude"]]
    )
    if df.empty:
        st.info("No locations with coordinates yet.")
    else:
        st.map(df, latitude="latitude", longitude="longitude")
        st.dataframe(df, hide_index=True, use_container_width=True)

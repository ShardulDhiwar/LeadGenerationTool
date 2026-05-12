# config/areas_coords.py
# ─────────────────────────────────────────────────────────────
# Lat/Lng center points for each area.
# Used to build a Google Maps URL with a fixed zoom level
# so results stay within ~7-14 km of the area center.
#
# Zoom level guide (approximate radius shown on screen):
#   14  →  ~2-3 km   (very tight, single neighborhood)
#   13  →  ~5-7 km   (one area)
#   12  →  ~10-14 km (MR travel-friendly)  ← DEFAULT
#   11  →  ~20-25 km (too wide)
# ─────────────────────────────────────────────────────────────

DEFAULT_ZOOM = 12   # ~10-14 km radius — change to 13 for tighter

AREA_COORDS: dict[str, tuple[float, float]] = {
    # ── City-level fallback coords ─────────────────────────────
    # Used when area="" (city-wide search). Must match the city
    # name passed to scrape_google_maps(city=...) exactly.
    "Pune":           (18.5204, 73.8567),
    "Mumbai":         (19.0760, 72.8777),
    "Bangalore":      (12.9716, 77.5946),
    "Hyderabad":      (17.3850, 78.4867),
    "Delhi":          (28.6139, 77.2090),

    # ── Pune ──────────────────────────────────────────────────
    "Baner":          (18.5590, 73.7868),
    "Wakad":          (18.5975, 73.7617),
    "Kothrud":        (18.5074, 73.8077),
    "Viman Nagar":    (18.5679, 73.9143),
    "Hadapsar":       (18.5018, 73.9260),
    "Aundh":          (18.5590, 73.8079),
    "Shivajinagar":   (18.5308, 73.8474),
    "Katraj":         (18.4529, 73.8654),
    "Hinjewadi":      (18.5912, 73.7385),
    "Koregaon Park":  (18.5362, 73.8938),
    "Chikhali":       (18.6476, 73.8074),

    # ── Mumbai ────────────────────────────────────────────────
    "Bandra":         (19.0596, 72.8295),
    "Andheri":        (19.1136, 72.8697),
    "Powai":          (19.1176, 72.9060),
    "Borivali":       (19.2307, 72.8567),
    "Thane":          (19.2183, 72.9781),
    "Dadar":          (19.0178, 72.8478),
    "Kurla":          (19.0728, 72.8826),
    "Malad":          (19.1863, 72.8484),

    # ── Bangalore ─────────────────────────────────────────────
    "Whitefield":     (12.9698, 77.7500),
    "Koramangala":    (12.9352, 77.6245),
    "Indiranagar":    (12.9784, 77.6408),
    "HSR Layout":     (12.9116, 77.6474),
    "Jayanagar":      (12.9252, 77.5938),

    # ── Hyderabad ─────────────────────────────────────────────
    "Banjara Hills":  (17.4156, 78.4347),
    "Gachibowli":     (17.4401, 78.3489),
    "Kondapur":       (17.4600, 78.3615),
    "Madhapur":       (17.4486, 78.3908),

    # ── Delhi ─────────────────────────────────────────────────
    "Connaught Place":(28.6315, 77.2167),
    "Dwarka":         (28.5921, 77.0460),
    "Rohini":         (28.7041, 77.1025),
    "Saket":          (28.5244, 77.2066),
    "Lajpat Nagar":   (28.5677, 77.2433),
}
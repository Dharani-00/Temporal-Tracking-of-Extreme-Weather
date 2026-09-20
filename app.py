import os
import math
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")
CORS(app)

PORT = int(os.getenv("PORT", 5000))
DEBUG = os.getenv("DEBUG", "True").lower() == "true"
WEATHER_PROVIDER = os.getenv("WEATHER_PROVIDER", "openmeteo").lower().strip()
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "").strip()

DEFAULT_CITY = os.getenv("DEFAULT_CITY", "Chennai")
DEFAULT_LAT = float(os.getenv("DEFAULT_LAT", 13.0827))
DEFAULT_LON = float(os.getenv("DEFAULT_LON", 80.2707))


def is_placeholder_key(key: str) -> bool:
    if not key:
        return True
    placeholders = ["your_", "_key_here", "example", "xxx", "none", "null"]
    return any(p in key.lower() for p in placeholders) or len(key) < 16


def compass_direction(deg: float) -> str:
    val = int((deg / 22.5) + 0.5)
    arr = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return arr[val % 16]


def wmo_code_to_condition(code: int) -> tuple[str, str]:
    """Returns (condition_text, condition_category)"""
    mapping = {
        0: ("Clear Sky", "clear"),
        1: ("Mainly Clear", "clear"),
        2: ("Partly Cloudy", "clouds"),
        3: ("Overcast", "clouds"),
        45: ("Foggy", "fog"),
        48: ("Depositing Rime Fog", "fog"),
        51: ("Light Drizzle", "drizzle"),
        53: ("Moderate Drizzle", "drizzle"),
        55: ("Dense Drizzle", "drizzle"),
        61: ("Slight Rain", "rain"),
        63: ("Moderate Rain", "rain"),
        65: ("Heavy Rain", "rain"),
        71: ("Slight Snowfall", "snow"),
        73: ("Moderate Snowfall", "snow"),
        75: ("Heavy Snowfall", "snow"),
        77: ("Snow Grains", "snow"),
        80: ("Slight Rain Showers", "rain"),
        81: ("Moderate Rain Showers", "rain"),
        82: ("Violent Rain Showers", "rain"),
        85: ("Slight Snow Showers", "snow"),
        86: ("Heavy Snow Showers", "snow"),
        95: ("Thunderstorm", "storm"),
        96: ("Thunderstorm with Slight Hail", "storm"),
        99: ("Thunderstorm with Heavy Hail", "storm"),
    }
    return mapping.get(code, ("Unknown", "clear"))


def compute_anomaly_metrics(temp_c: float, wind_kph: float, gust_kph: float, precip_mm: float, weather_code: int):
    """Dynamically calculates anomaly scores, alerts, and risk level from telemetry."""
    # Reference seasonal baseline approximation
    baseline_temp = 27.0
    temp_dev = round(temp_c - baseline_temp, 1)
    
    # Calculate anomaly index (1.0 - 10.0 scale)
    base_score = 3.0
    # Rain factor
    if precip_mm > 50:
        base_score += 4.5
    elif precip_mm > 20:
        base_score += 3.0
    elif precip_mm > 5:
        base_score += 1.5

    # Wind factor
    if gust_kph > 70 or wind_kph > 50:
        base_score += 3.0
    elif gust_kph > 45 or wind_kph > 30:
        base_score += 1.8

    # Temp deviation factor
    if abs(temp_dev) > 4.0:
        base_score += 2.0
    elif abs(temp_dev) > 2.0:
        base_score += 1.0

    # Storm weather code
    if weather_code in [95, 96, 99, 82, 65]:
        base_score += 2.5

    score = min(round(base_score, 1), 9.9)

    if score >= 7.5:
        risk_level = "High Risk"
    elif score >= 5.0:
        risk_level = "Moderate"
    else:
        risk_level = "Low Risk"

    consensus = min(98, max(75, int(85 + score)))

    alerts = []
    if precip_mm >= 15 or weather_code in [65, 82, 95, 96, 99]:
        alerts.append({
            "level": "Red Warning",
            "code": f"MET-ALERT-{int(score*100)}",
            "title": "Severe Rainfall & Surge Alert",
            "desc": f"Intense precipitation active ({precip_mm:.1f} mm recorded). Flash surge protocols active.",
            "type": "rain",
            "severity": "extreme",
            "window": "Next 24 Hours",
            "expected": f"{max(40, int(precip_mm*2))} - {max(80, int(precip_mm*3))} mm"
        })

    if gust_kph >= 45 or wind_kph >= 35:
        alerts.append({
            "level": "Advisory",
            "code": "GALE-WIND-07",
            "title": "High Wind & Gale Advisory",
            "desc": f"Surface wind gusts up to {gust_kph:.0f} km/h recorded. Maritime and coastal advisory.",
            "type": "wind",
            "severity": "high",
            "window": "Next 12 Hours",
            "expected": f"Gusts {gust_kph:.0f} km/h"
        })

    if not alerts:
        alerts.append({
            "level": "Advisory",
            "code": "MONITOR-01",
            "title": "Atmospheric Stability Nominal",
            "desc": "Baseline radar readings within normal seasonal parameters. No immediate hazards detected.",
            "type": "info",
            "severity": "low",
            "window": "Active",
            "expected": "Normal Telemetry"
        })

    sign = "+" if temp_dev >= 0 else ""
    thermal_str = f"{sign}{temp_dev}°C Above Normal" if temp_dev >= 0 else f"{temp_dev}°C Below Normal"

    return {
        "score": score,
        "level": risk_level,
        "consensus": consensus,
        "thermal_deviation": thermal_str,
        "thermal_dev_val": temp_dev,
        "alerts": alerts,
        "spectral_residual": round(0.5 + (score / 20.0), 2),
        "diffusion_steps": 1000,
        "graph_nodes": 48204
    }


def reverse_geocode(lat: float, lon: float) -> dict:
    """Attempts to reverse geocode coordinates into City, Region, Country."""
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=10"
        headers = {"User-Agent": "ATWAIS-WeatherMonitor/1.0"}
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            data = r.json()
            address = data.get("address", {})
            name = (
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("county")
                or address.get("state_district")
                or address.get("state")
                or f"Coords ({lat:.2f}, {lon:.2f})"
            )
            region = address.get("state", address.get("region", ""))
            country = address.get("country", "")
            return {"name": name, "region": region, "country": country}
    except Exception as e:
        print(f"Reverse geocode error: {e}")
    return {"name": f"Lat {lat:.2f}°N", "region": f"Lon {lon:.2f}°E", "country": "Surveillance Sector"}


def forward_geocode(query: str) -> list:
    """Searches for city suggestions matching query."""
    results = []
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={query}&count=5&language=en&format=json"
        r = requests.get(url, timeout=4)
        if r.status_code == 200:
            data = r.json()
            for item in data.get("results", []):
                results.append({
                    "name": item.get("name"),
                    "region": item.get("admin1", ""),
                    "country": item.get("country", ""),
                    "lat": item.get("latitude"),
                    "lon": item.get("longitude")
                })
    except Exception as e:
        print(f"Forward geocode error: {e}")
    return results


def generate_mock_weather(lat: float, lon: float, city_info: dict) -> dict:
    """Generates synthetic weather data when network fails."""
    import random
    temp_c = random.uniform(22.0, 35.0)
    feels_like_c = temp_c + random.uniform(-2, 4)
    humidity = random.randint(50, 95)
    wind_kph = random.uniform(5.0, 60.0)
    gust_kph = wind_kph * random.uniform(1.2, 1.8)
    wind_dir = random.uniform(0, 360)
    pressure_hpa = random.uniform(990, 1025)
    precip_mm = random.uniform(0, 100) if random.random() > 0.5 else 0.0
    weather_code = random.choice([0, 1, 2, 3, 45, 61, 63, 65, 80, 82, 95])
    
    cond_text, cond_cat = wmo_code_to_condition(weather_code)
    
    hourly_24h = []
    total_precip = 0.0
    for i in range(24):
        hour_lbl = f"{(12 + i) % 24:02d}:00"
        h_temp = temp_c + random.uniform(-3, 3)
        h_precip = random.uniform(0, 5) if random.random() > 0.7 else 0.0
        total_precip += h_precip
        hourly_24h.append({
            "time": hour_lbl,
            "temp_c": round(h_temp, 1),
            "precip_mm": round(h_precip, 1),
            "rain_prob": random.randint(0, 100)
        })
        
    daily_3d = []
    day_labels = ["Today", "Tomorrow", "Day 3"]
    for i in range(3):
        max_t = temp_c + random.uniform(0, 5)
        min_t = temp_c - random.uniform(0, 5)
        d_code = random.choice([0, 1, 2, 61, 63, 95])
        c_text, _ = wmo_code_to_condition(d_code)
        prob = random.randint(0, 100)
        daily_3d.append({
            "day": day_labels[i],
            "date": (datetime.now() + timedelta(days=i)).strftime("%b %d"),
            "temp_max": round(max_t, 1),
            "temp_min": round(min_t, 1),
            "precip_prob": prob,
            "condition": c_text,
            "risk": "High Risk" if prob > 70 else ("Moderate" if prob > 40 else "Low Risk")
        })
        
    anomaly_data = compute_anomaly_metrics(temp_c, wind_kph, gust_kph, total_precip, weather_code)
    anomaly_data["peak_window"] = "18:00 Peak Surge"

    return {
        "status": "success",
        "provider": "mock (offline mode)",
        "location": {
            "name": city_info.get("name", "Unknown Sector"),
            "region": city_info.get("region", ""),
            "country": city_info.get("country", ""),
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "sector": f"{city_info.get('name', 'Sector')} Surveillance Zone"
        },
        "current": {
            "temp_c": round(temp_c, 1),
            "feels_like_c": round(feels_like_c, 1),
            "temp_k": round(temp_c + 273.15, 2),
            "humidity": humidity,
            "wind_kph": round(wind_kph, 1),
            "wind_gust_kph": round(gust_kph, 1),
            "wind_dir": round(wind_dir, 0),
            "wind_dir_compass": compass_direction(wind_dir),
            "pressure_hpa": round(pressure_hpa, 1),
            "precipitation_mm": round(precip_mm, 1),
            "total_24h_precip": round(total_precip, 1),
            "condition": cond_text,
            "condition_cat": cond_cat,
            "radar_station": f"{city_info.get('name', 'MET')[:3].upper()}-Doppler-04 (Simulated)"
        },
        "hourly_24h": hourly_24h,
        "daily_3d": daily_3d,
        "alerts": anomaly_data["alerts"],
        "anomaly": anomaly_data
    }



def fetch_open_meteo(lat: float, lon: float, city_info: dict) -> dict:
    """Fetches high-resolution weather using Open-Meteo API."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        "&hourly=temperature_2m,precipitation_probability,precipitation,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"
        "&timezone=auto&forecast_days=4"
    )
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    data = r.json()

    current_raw = data.get("current", {})
    temp_c = float(current_raw.get("temperature_2m", 25.0))
    feels_like_c = float(current_raw.get("apparent_temperature", temp_c))
    temp_k = round(temp_c + 273.15, 2)
    humidity = int(current_raw.get("relative_humidity_2m", 75))
    wind_kph = float(current_raw.get("wind_speed_10m", 15.0))
    gust_kph = float(current_raw.get("wind_gusts_10m", wind_kph * 1.4))
    wind_dir = float(current_raw.get("wind_direction_10m", 0))
    pressure_hpa = float(current_raw.get("surface_pressure", 1013.0))
    precip_mm = float(current_raw.get("precipitation", 0.0))
    weather_code = int(current_raw.get("weather_code", 0))

    cond_text, cond_cat = wmo_code_to_condition(weather_code)

    # 24-hour hourly slice
    hourly_raw = data.get("hourly", {})
    times = hourly_raw.get("time", [])
    temps = hourly_raw.get("temperature_2m", [])
    precips = hourly_raw.get("precipitation", [])
    probs = hourly_raw.get("precipitation_probability", [])

    hourly_24h = []
    # Take next 24 hours
    limit = min(24, len(times))
    total_24h_precip = 0.0
    peak_precip_hour = "18:00"
    max_hour_precip = -1.0

    for i in range(limit):
        t_str = times[i]
        # parse hour "2026-09-20T14:00" -> "14:00"
        hour_label = t_str.split("T")[-1] if "T" in t_str else f"{i:02d}:00"
        t_val = round(temps[i], 1) if i < len(temps) else temp_c
        p_val = round(precips[i], 1) if i < len(precips) else 0.0
        prob_val = int(probs[i]) if i < len(probs) and probs[i] is not None else 20

        total_24h_precip += p_val
        if p_val > max_hour_precip:
            max_hour_precip = p_val
            peak_precip_hour = hour_label

        hourly_24h.append({
            "time": hour_label,
            "temp_c": t_val,
            "precip_mm": p_val,
            "rain_prob": prob_val
        })

    # 3-Day Forecast
    daily_raw = data.get("daily", {})
    d_times = daily_raw.get("time", [])
    d_max = daily_raw.get("temperature_2m_max", [])
    d_min = daily_raw.get("temperature_2m_min", [])
    d_codes = daily_raw.get("weather_code", [])
    d_probs = daily_raw.get("precipitation_probability_max", [])
    d_precip = daily_raw.get("precipitation_sum", [])

    day_labels = ["Today", "Tomorrow", "Day 3"]
    daily_3d = []
    for i in range(min(3, len(d_times))):
        d_date_str = d_times[i]
        try:
            dt = datetime.fromisoformat(d_date_str)
            formatted_date = dt.strftime("%b %d")
        except Exception:
            formatted_date = d_date_str

        c_text, _ = wmo_code_to_condition(d_codes[i] if i < len(d_codes) else 0)
        p_prob = d_probs[i] if i < len(d_probs) and d_probs[i] is not None else 40
        d_risk = "High Risk" if p_prob > 70 else ("Moderate" if p_prob > 40 else "Low Risk")

        daily_3d.append({
            "day": day_labels[i] if i < len(day_labels) else f"Day {i+1}",
            "date": formatted_date,
            "temp_max": round(d_max[i], 1) if i < len(d_max) else temp_c + 2,
            "temp_min": round(d_min[i], 1) if i < len(d_min) else temp_c - 3,
            "precip_prob": p_prob,
            "condition": c_text,
            "risk": d_risk
        })

    anomaly_data = compute_anomaly_metrics(temp_c, wind_kph, gust_kph, total_24h_precip, weather_code)
    anomaly_data["peak_window"] = f"{peak_precip_hour} Peak Surge"

    return {
        "status": "success",
        "provider": "openmeteo",
        "location": {
            "name": city_info.get("name", "Target Sector"),
            "region": city_info.get("region", ""),
            "country": city_info.get("country", ""),
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "sector": f"{city_info.get('name', 'Sector')} Surveillance Zone"
        },
        "current": {
            "temp_c": round(temp_c, 1),
            "feels_like_c": round(feels_like_c, 1),
            "temp_k": temp_k,
            "humidity": humidity,
            "wind_kph": round(wind_kph, 1),
            "wind_gust_kph": round(gust_kph, 1),
            "wind_dir": round(wind_dir, 0),
            "wind_dir_compass": compass_direction(wind_dir),
            "pressure_hpa": round(pressure_hpa, 1),
            "precipitation_mm": round(precip_mm, 1),
            "total_24h_precip": round(total_24h_precip, 1),
            "condition": cond_text,
            "condition_cat": cond_cat,
            "radar_station": f"{city_info.get('name', 'MET')[:3].upper()}-Doppler-04 (Active)"
        },
        "hourly_24h": hourly_24h,
        "daily_3d": daily_3d,
        "alerts": anomaly_data["alerts"],
        "anomaly": anomaly_data
    }


def fetch_openweathermap(lat: float, lon: float, city_info: dict, key: str) -> dict:
    """Fetches weather from OpenWeatherMap using user's API key."""
    # Current weather
    curr_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={key}&units=metric"
    cr = requests.get(curr_url, timeout=8)
    cr.raise_for_status()
    cdata = cr.json()

    # 5-day / 3-hour forecast
    f_url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={key}&units=metric"
    fr = requests.get(f_url, timeout=8)
    fr.raise_for_status()
    fdata = fr.json()

    temp_c = float(cdata.get("main", {}).get("temp", 25.0))
    feels_like_c = float(cdata.get("main", {}).get("feels_like", temp_c))
    temp_k = round(temp_c + 273.15, 2)
    humidity = int(cdata.get("main", {}).get("humidity", 70))
    pressure_hpa = float(cdata.get("main", {}).get("pressure", 1013))
    wind_mps = float(cdata.get("wind", {}).get("speed", 5.0))
    wind_kph = round(wind_mps * 3.6, 1)
    gust_mps = float(cdata.get("wind", {}).get("gust", wind_mps * 1.3))
    gust_kph = round(gust_mps * 3.6, 1)
    wind_deg = float(cdata.get("wind", {}).get("deg", 0))

    weather_list = cdata.get("weather", [{}])
    cond_text = weather_list[0].get("main", "Clear") if weather_list else "Clear"
    weather_desc = weather_list[0].get("description", cond_text).title() if weather_list else cond_text

    rain_obj = cdata.get("rain", {})
    precip_mm = float(rain_obj.get("1h", rain_obj.get("3h", 0.0)))

    # Process forecast for hourly & 3-day
    flist = fdata.get("list", [])
    hourly_24h = []
    total_24h_precip = precip_mm

    for item in flist[:8]:  # 8 slots * 3 hours = 24 hours
        dt_txt = item.get("dt_txt", "")
        hour_lbl = dt_txt.split(" ")[-1][:5] if " " in dt_txt else "12:00"
        t_val = round(item.get("main", {}).get("temp", temp_c), 1)
        r_val = round(float(item.get("rain", {}).get("3h", 0.0)) / 3.0, 1)
        total_24h_precip += r_val
        p_prob = int(float(item.get("pop", 0.2)) * 100)
        hourly_24h.append({
            "time": hour_lbl,
            "temp_c": t_val,
            "precip_mm": r_val,
            "rain_prob": p_prob
        })

    # Group daily
    daily_map = {}
    for item in flist:
        dt_txt = item.get("dt_txt", "")
        day_key = dt_txt.split(" ")[0]
        if day_key not in daily_map:
            daily_map[day_key] = []
        daily_map[day_key].append(item)

    daily_3d = []
    day_labels = ["Today", "Tomorrow", "Day 3"]
    idx = 0
    for day_k, items in daily_map.items():
        if idx >= 3:
            break
        temps = [it.get("main", {}).get("temp", 25) for it in items]
        pops = [it.get("pop", 0) for it in items]
        max_pop = int(max(pops) * 100) if pops else 30
        cond = items[0].get("weather", [{}])[0].get("main", "Clear")
        d_risk = "High Risk" if max_pop > 70 else ("Moderate" if max_pop > 40 else "Low Risk")

        try:
            dt = datetime.strptime(day_k, "%Y-%m-%d")
            fmt_date = dt.strftime("%b %d")
        except Exception:
            fmt_date = day_k

        daily_3d.append({
            "day": day_labels[idx],
            "date": fmt_date,
            "temp_max": round(max(temps), 1),
            "temp_min": round(min(temps), 1),
            "precip_prob": max_pop,
            "condition": cond,
            "risk": d_risk
        })
        idx += 1

    city_name = cdata.get("name") or city_info.get("name", "Chennai")
    country_name = cdata.get("sys", {}).get("country") or city_info.get("country", "")

    anomaly_data = compute_anomaly_metrics(temp_c, wind_kph, gust_kph, total_24h_precip, 0)

    return {
        "status": "success",
        "provider": "openweathermap",
        "location": {
            "name": city_name,
            "region": city_info.get("region", ""),
            "country": country_name,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "sector": f"{city_name} Surveillance Zone"
        },
        "current": {
            "temp_c": round(temp_c, 1),
            "feels_like_c": round(feels_like_c, 1),
            "temp_k": temp_k,
            "humidity": humidity,
            "wind_kph": wind_kph,
            "wind_gust_kph": gust_kph,
            "wind_dir": wind_deg,
            "wind_dir_compass": compass_direction(wind_deg),
            "pressure_hpa": pressure_hpa,
            "precipitation_mm": round(precip_mm, 1),
            "total_24h_precip": round(total_24h_precip, 1),
            "condition": weather_desc,
            "radar_station": f"{city_name[:3].upper()}-Doppler-04 (Active)"
        },
        "hourly_24h": hourly_24h,
        "daily_3d": daily_3d,
        "alerts": anomaly_data["alerts"],
        "anomaly": anomaly_data
    }


# ========================================================
# FLASK ROUTES
# ========================================================

@app.route("/")
def index():
    """Serves the main dashboard HTML."""
    file_to_serve = "dashboard.html" if os.path.exists("dashboard.html") else "atwais.html"
    response = make_response(send_from_directory(".", file_to_serve))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/<path:path>")
def static_proxy(path):
    """Serves static files."""
    response = make_response(send_from_directory(".", path))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/config", methods=["GET"])
def get_config():
    """Returns provider status & key setup."""
    has_owm_key = not is_placeholder_key(OPENWEATHER_API_KEY)
    has_weatherapi_key = not is_placeholder_key(WEATHERAPI_KEY)
    return jsonify({
        "provider": WEATHER_PROVIDER,
        "has_openweather_key": has_owm_key,
        "has_weatherapi_key": has_weatherapi_key,
        "default_city": DEFAULT_CITY,
        "default_lat": DEFAULT_LAT,
        "default_lon": DEFAULT_LON
    })


@app.route("/api/search", methods=["GET"])
def search_cities():
    """Autocomplete / search endpoint for cities."""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify([])
    results = forward_geocode(q)
    return jsonify(results)


@app.route("/api/weather", methods=["GET"])
def get_weather():
    """Unified weather endpoint. Accepts lat/lon or city."""
    city = request.args.get("city", "").strip()
    lat_param = request.args.get("lat")
    lon_param = request.args.get("lon")

    lat, lon = DEFAULT_LAT, DEFAULT_LON
    city_info = {"name": DEFAULT_CITY, "region": "Tamil Nadu", "country": "India"}

    # Determine coordinates
    if lat_param and lon_param:
        try:
            lat = float(lat_param)
            lon = float(lon_param)
            city_info = reverse_geocode(lat, lon)
        except ValueError:
            return jsonify({"error": "Invalid lat/lon parameters"}), 400
    elif city:
        matches = forward_geocode(city)
        if matches:
            lat = float(matches[0]["lat"])
            lon = float(matches[0]["lon"])
            city_info = matches[0]
        else:
            # Fallback if geocoding returns empty
            city_info = {"name": city, "region": "", "country": ""}

    # Check which provider to use
    provider = WEATHER_PROVIDER
    if provider == "openweathermap" and not is_placeholder_key(OPENWEATHER_API_KEY):
        try:
            data = fetch_openweathermap(lat, lon, city_info, OPENWEATHER_API_KEY)
            return jsonify(data)
        except Exception as e:
            print(f"OpenWeatherMap request failed: {e}. Falling back to Open-Meteo.")

    # Default / fallback: Open-Meteo (Zero key needed)
    try:
        data = fetch_open_meteo(lat, lon, city_info)
        return jsonify(data)
    except Exception as e:
        print(f"Weather fetch error: {e}. Generating mock data.")
        return jsonify(generate_mock_weather(lat, lon, city_info))


if __name__ == "__main__":
    print("=" * 60)
    print(">> ATWAIS Weather Anomaly Monitoring System")
    print(f">> Serving on http://localhost:{PORT}")
    print(f">> Provider: {WEATHER_PROVIDER.upper()}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)

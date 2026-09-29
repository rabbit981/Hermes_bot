"""
Mausam: Open-Meteo API (free, kisi API key ki zaroorat nahi).
Default shehar WEATHER_CITY environment variable se badal sakte hain.
"""
import html
import os

import requests

DEFAULT_CITY = os.environ.get("WEATHER_CITY", "Patna")
_GEO_CACHE = {}

_CODES = {
    0: ("☀️", "Saaf aasman"), 1: ("🌤️", "Mostly saaf"), 2: ("⛅", "Thoda badal"),
    3: ("☁️", "Badal chhaye"), 45: ("🌫️", "Kohra"), 48: ("🌫️", "Ghana kohra"),
    51: ("🌦️", "Halki phuhaar"), 53: ("🌦️", "Phuhaar"), 55: ("🌦️", "Tez phuhaar"),
    56: ("🌧️", "Thandi phuhaar"), 57: ("🌧️", "Thandi phuhaar"),
    61: ("🌧️", "Halki barish"), 63: ("🌧️", "Barish"), 65: ("🌧️", "Tez barish"),
    66: ("🌧️", "Thandi barish"), 67: ("🌧️", "Thandi tez barish"),
    71: ("🌨️", "Halki barf"), 73: ("🌨️", "Barf"), 75: ("🌨️", "Tez barf"),
    77: ("🌨️", "Barf ke kan"), 80: ("🌦️", "Halki bauchhar"), 81: ("🌧️", "Bauchhar"),
    82: ("⛈️", "Tez bauchhar"), 85: ("🌨️", "Barf ki bauchhar"), 86: ("🌨️", "Tez barf ki bauchhar"),
    95: ("⛈️", "Garaj ke saath barish"), 96: ("⛈️", "Garaj aur ole"), 99: ("⛈️", "Garaj aur tez ole"),
}


def _desc(code):
    return _CODES.get(int(code), ("🌡️", "Mausam data"))


def _geocode(city):
    key = city.strip().lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    r = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"}, timeout=10)
    res = (r.json().get("results") or [])
    if not res:
        raise ValueError(f"'{city}' shehar nahi mila")
    g = res[0]
    place = ", ".join(x for x in (g.get("name"), g.get("admin1")) if x)
    _GEO_CACHE[key] = (place, g["latitude"], g["longitude"])
    return _GEO_CACHE[key]


def get_weather(city=None):
    city = (city or DEFAULT_CITY).strip()
    place, lat, lon = _geocode(city)
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata", "forecast_days": 2,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        }, timeout=10)
    d = r.json()
    if "current" not in d:
        raise ValueError("Mausam data nahi mila")
    return place, d


def _day_line(label, d, i):
    icon, txt = _desc(d["daily"]["weather_code"][i])
    return (f"{label}: {icon} {txt} | {d['daily']['temperature_2m_min'][i]:.0f}°-"
            f"{d['daily']['temperature_2m_max'][i]:.0f}°C | Barish ka chance "
            f"{d['daily']['precipitation_probability_max'][i] or 0:.0f}%")


def format_html(city=None):
    place, d = get_weather(city)
    c = d["current"]
    icon, txt = _desc(c["weather_code"])
    return (
        f"{icon} <b>{html.escape(place)}</b> ka mausam\n"
        f"Abhi: <b>{c['temperature_2m']:.0f}°C</b> (mehsoos {c['apparent_temperature']:.0f}°C) | "
        f"{txt}\n"
        f"Nami {c['relative_humidity_2m']:.0f}% | Hawa {c['wind_speed_10m']:.0f} km/h\n"
        f"─────────────────────\n"
        f"{_day_line('Aaj', d, 0)}\n{_day_line('Kal', d, 1)}"
    )


def context_text(city=None):
    """Hermes ke liye plain text."""
    place, d = get_weather(city)
    c = d["current"]
    return (f"{place}: abhi {c['temperature_2m']:.0f}C (mehsoos {c['apparent_temperature']:.0f}C), "
            f"{_desc(c['weather_code'])[1]}, nami {c['relative_humidity_2m']:.0f}%, "
            f"hawa {c['wind_speed_10m']:.0f} km/h. {_day_line('Aaj', d, 0)}. {_day_line('Kal', d, 1)}")

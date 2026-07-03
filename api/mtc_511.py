"""
MTC / 511.org live Bay Area traffic — incidents & construction.

Additive and fully optional: this is a standalone, cached, fail-soft client for
the regional 511.org open-data feed (https://511.org/open-data/traffic). It
covers San Francisco and Alameda County (Oakland/Berkeley) roads, complementing
the static Caltrans/city datasets in api/traffic.py. Nothing here touches the
core routing path — it is exposed as its own endpoint so a network hiccup with
511 can never slow down or break street lookups or route calculation.

Config (env):
  MTC_511_TOKEN   511.org API token. When unset, the endpoint reports
                  {"available": false} and returns an empty list — no errors.

Endpoint:
  GET /api/traffic/bay/incidents            all active Bay Area events
  GET /api/traffic/bay/incidents?road=I-80  filter by road-name substring
"""

import os
import time
import threading

import requests
from flask import Blueprint, request, jsonify

mtc_511_api = Blueprint('mtc_511_api', __name__, url_prefix='/api/traffic')

_API_URL = 'http://api.511.org/traffic/events'
_TIMEOUT = 10           # seconds — never block the caller for long
_CACHE_TTL = 300        # 5 min; 511 data is coarse and rate-limited

# Simple thread-safe in-memory cache so we do not hammer the rate-limited API.
_cache = {'ts': 0.0, 'events': None}
_lock = threading.Lock()


def _token():
    return os.environ.get('MTC_511_TOKEN') or os.environ.get('MTC_511_API_KEY')


def _simplify(event):
    """Reduce a raw 511 event to the fields the frontend actually needs."""
    roads = event.get('roads') or []
    geo = event.get('geography') or {}
    return {
        'id': event.get('id'),
        'headline': event.get('headline'),
        'type': event.get('event_type'),          # CONSTRUCTION / INCIDENT / ...
        'severity': event.get('severity'),
        'roads': [r.get('name') for r in roads if r.get('name')],
        'directions': [r.get('direction') for r in roads if r.get('direction')],
        'lane_status': [r.get('state') for r in roads if r.get('state')],
        'geometry': geo,                            # GeoJSON Point/LineString
        'updated': event.get('updated') or event.get('created'),
    }


def _fetch_raw():
    """Fetch + normalize all active events. Returns [] on any problem."""
    token = _token()
    if not token:
        return None  # signals "not configured"
    try:
        # requests transparently gunzips 511's gzip responses.
        resp = requests.get(
            _API_URL,
            params={'api_key': token, 'format': 'json'},
            timeout=_TIMEOUT,
            headers={'Accept-Encoding': 'gzip'},
        )
        if resp.status_code != 200:
            return []
        # 511 prefixes a UTF-8 BOM; requests' .json() can choke on it, so decode
        # explicitly with utf-8-sig.
        import json
        data = json.loads(resp.content.decode('utf-8-sig'))
        events = data.get('events') if isinstance(data, dict) else data
        if not events:
            return []
        return [_simplify(e) for e in events]
    except Exception as e:  # network error, timeout, bad JSON — degrade quietly
        print(f"⚠️ 511 fetch failed: {e}")
        return []


def get_bay_area_events(road=None, force=False):
    """Cached accessor. Returns (events_list, available_bool)."""
    now = time.time()
    with _lock:
        fresh = _cache['events'] is not None and (now - _cache['ts']) < _CACHE_TTL
        if force or not fresh:
            fetched = _fetch_raw()
            if fetched is None:
                return [], False  # not configured
            _cache['events'] = fetched
            _cache['ts'] = now
        events = _cache['events'] or []

    if road:
        needle = road.strip().upper()
        events = [e for e in events
                  if any(needle in (name or '').upper() for name in e['roads'])
                  or needle in (e.get('headline') or '').upper()]
    return events, True


@mtc_511_api.route('/bay/incidents', methods=['GET'])
def bay_incidents():
    road = request.args.get('road')
    events, available = get_bay_area_events(road=road)
    return jsonify({
        'available': available,
        'source': 'MTC / 511.org',
        'count': len(events),
        'road_filter': road,
        'incidents': events,
    })

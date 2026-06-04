import pandas as pd
import os
import re
from flask import Blueprint, request, jsonify
from flask_restful import Api, Resource

# Blueprint for traffic API
traffic_api = Blueprint('traffic', __name__, url_prefix='')
api = Api(traffic_api)


class TrafficData:
    """
    Traffic data handler that combines two San Diego datasets:

    1. City of San Diego traffic counts (traffic_counts_datasd.csv) — historical
       measured vehicle counts per street. This is the authoritative source and
       is used whenever a street is covered by it.

    2. San Diego County road network (Roads_All.csv) — a county-wide road
       inventory (~25k unique road names) that includes posted speed limits but
       no measured counts. It is used as a fallback to estimate congestion for
       streets the city dataset does not cover, extending route adjustments to
       the entire county instead of just the city.
    """

    # Traffic thresholds based on typical daily vehicle counts
    # These are calibrated for San Diego street data
    TRAFFIC_THRESHOLDS = {
        'very_low': 3000,      # < 3000 vehicles/day
        'low': 8000,           # 3000-8000 vehicles/day
        'moderate': 15000,     # 8000-15000 vehicles/day
        'high': 25000,         # 15000-25000 vehicles/day
        'very_high': float('inf')  # > 25000 vehicles/day
    }

    # Congestion multipliers for travel time adjustment
    CONGESTION_MULTIPLIERS = {
        'very_low': 0.90,   # 10% faster than baseline
        'low': 0.95,        # 5% faster than baseline
        'moderate': 1.0,    # baseline
        'high': 1.15,       # 15% slower
        'very_high': 1.30   # 30% slower
    }

    # County roads have no measured counts, so we estimate a congestion level
    # from the posted speed limit. Higher-speed roads (highways/arterials) carry
    # heavier volume and are more congestion-prone; low-speed roads are
    # residential/local and typically faster than baseline. This mirrors the
    # "more traffic => slower" mapping used for the city counts.
    COUNTY_SPEED_THRESHOLDS = [
        (55, 'high'),       # >= 55 mph: freeways / highways
        (40, 'moderate'),   # 40-54 mph: major arterials
        (25, 'low'),        # 25-39 mph: collectors / minor arterials
        (0,  'very_low'),   # < 25 mph: residential / local streets
    ]

    def __init__(self):
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        city_path = os.path.abspath(os.path.join(data_dir, 'traffic_counts_datasd.csv'))
        county_path = os.path.abspath(os.path.join(data_dir, 'Roads_All.csv'))

        # City of San Diego measured traffic counts (authoritative)
        self.traffic_df = self._load_data(city_path)
        self._build_street_index()

        # San Diego County road network (fallback coverage)
        self.county_df = self._load_county_data(county_path)
        self._build_county_index()

    def _load_data(self, path):
        """Load and preprocess the traffic CSV data."""
        try:
            df = pd.read_csv(path)
            required_cols = ['street_name', 'total_count']
            
            if not all(col in df.columns for col in required_cols):
                print(f"⚠️ Missing required columns. Found: {df.columns.tolist()}")
                return pd.DataFrame()
            
            # Clean and normalize data
            df['street_name'] = df['street_name'].astype(str).str.upper().str.strip()
            df['total_count'] = pd.to_numeric(df['total_count'], errors='coerce').fillna(0)
            
            # Parse date for recency weighting
            if 'date_count' in df.columns:
                df['date_count'] = pd.to_datetime(df['date_count'], errors='coerce')
            
            # Clean limits column for intersection matching
            if 'limits' in df.columns:
                df['limits'] = df['limits'].astype(str).str.upper().str.strip()
            
            print(f"✅ Loaded {len(df)} traffic records")
            return df
            
        except Exception as e:
            print(f"⚠️ Error loading traffic CSV: {e}")
            return pd.DataFrame()

    def _build_street_index(self):
        """Build an index of unique street names for faster lookup."""
        if self.traffic_df.empty:
            self.street_index = {}
            return
            
        # Group by street name and calculate aggregate stats
        self.street_index = {}
        for street_name in self.traffic_df['street_name'].unique():
            street_data = self.traffic_df[self.traffic_df['street_name'] == street_name]
            
            # Get most recent data (weighted average favoring recent counts)
            if 'date_count' in street_data.columns:
                sorted_data = street_data.sort_values('date_count', ascending=False)
                # Weight recent data more heavily
                recent_avg = sorted_data.head(3)['total_count'].mean()
            else:
                recent_avg = street_data['total_count'].mean()
            
            self.street_index[street_name] = {
                'avg_count': recent_avg,
                'max_count': street_data['total_count'].max(),
                'min_count': street_data['total_count'].min(),
                'sample_size': len(street_data)
            }

    def _load_county_data(self, path):
        """
        Load the county-wide road network (Roads_All.csv).

        Only the columns we need are read to keep memory/load time reasonable
        for this large file. Returns an empty DataFrame if the file is missing
        so the city dataset continues to work on its own.
        """
        try:
            if not os.path.exists(path):
                print("⚠️ County roads file not found; using city data only")
                return pd.DataFrame()

            wanted = {'RD30NAME', 'RD30SFX', 'RD30FULL', 'SPEED', 'FUNCLASS', 'ONEWAY'}
            df = pd.read_csv(path, usecols=lambda c: c in wanted, low_memory=False)

            if 'RD30NAME' not in df.columns:
                print(f"⚠️ County roads missing name column. Found: {df.columns.tolist()}")
                return pd.DataFrame()

            df['SPEED'] = pd.to_numeric(df.get('SPEED'), errors='coerce')

            # Build a human-readable street name (base name + suffix), e.g.
            # "STEVENS AVE", which we then normalize the same way as the city data.
            name = df['RD30NAME'].fillna('').astype(str).str.strip()
            sfx = df['RD30SFX'].fillna('').astype(str).str.strip() if 'RD30SFX' in df.columns else ''
            df['raw_name'] = (name + ' ' + sfx).str.strip()

            print(f"✅ Loaded {len(df)} county road segments")
            return df

        except Exception as e:
            print(f"⚠️ Error loading county roads CSV: {e}")
            return pd.DataFrame()

    def _build_county_index(self):
        """
        Build an index of unique county road names -> aggregate speed limit.

        Uses vectorized pandas operations (not a per-street loop) because the
        county dataset is large (~160k segments / ~25k unique names).
        """
        if self.county_df.empty:
            self.county_index = {}
            return

        df = self.county_df

        # Normalize once per unique raw name, then map back onto every row.
        unique_raw = [r for r in df['raw_name'].dropna().unique() if r]
        norm_map = {r: self._normalize_street_name(r) for r in unique_raw}
        norm_names = df['raw_name'].map(norm_map)

        work = pd.DataFrame({'norm_name': norm_names, 'SPEED': df['SPEED'].values})
        work = work[work['norm_name'].str.len() > 2]

        grouped = work.groupby('norm_name')
        avg_speed = grouped['SPEED'].mean()
        sample_size = grouped.size()

        self.county_index = {}
        for street_name, speed in avg_speed.items():
            self.county_index[street_name] = {
                'avg_speed': None if pd.isna(speed) else float(speed),
                'sample_size': int(sample_size[street_name])
            }

    def _normalize_street_name(self, street_name):
        """
        Normalize street names for better matching.
        Handles common abbreviations and variations.
        """
        if not street_name:
            return ""
            
        name = street_name.upper().strip()
        
        # Common abbreviations mapping. Includes both the long forms found in
        # Google Maps instructions and the shorter forms used in the county
        # dataset (e.g. AVE, BLVD, HWY) so both datasets normalize the same way.
        abbreviations = {
            'STREET': 'ST',
            'AVENUE': 'AV',
            'AVE': 'AV',
            'BOULEVARD': 'BL',
            'BLVD': 'BL',
            'DRIVE': 'DR',
            'ROAD': 'RD',
            'LANE': 'LN',
            'COURT': 'CT',
            'PLACE': 'PL',
            'HIGHWAY': 'HW',
            'HWY': 'HW',
            'FREEWAY': 'FW',
        }
        
        for full, abbrev in abbreviations.items():
            name = re.sub(rf'\b{full}\b', abbrev, name)
        
        # Remove extra whitespace
        name = ' '.join(name.split())
        
        return name

    def _extract_street_from_instruction(self, instruction):
        """
        Extract street name from a Google Maps instruction.
        Returns a list of potential street names to match.
        """
        if not instruction:
            return []
            
        instruction = instruction.upper()
        streets = []
        
        # Common patterns in Google Maps instructions
        patterns = [
            r'(?:ONTO|ON|TO)\s+([A-Z0-9\s]+?)(?:\s+(?:ST|AV|BL|DR|RD|LN|CT|PL|HW|FW))',
            r'(?:VIA|TAKE)\s+([A-Z0-9\s]+?)(?:\s+(?:ST|AV|BL|DR|RD|LN|CT|PL|HW|FW))',
            r'([A-Z0-9]+\s+(?:ST|AV|BL|DR|RD|LN|CT|PL|HW|FW))',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, instruction)
            streets.extend(matches)
        
        # Clean and deduplicate
        cleaned = []
        for s in streets:
            normalized = self._normalize_street_name(s)
            if normalized and len(normalized) > 2:
                cleaned.append(normalized)
        
        return list(set(cleaned))

    def get_traffic_count(self, street_name):
        """
        Get the average measured traffic count for a street from the city
        dataset. Returns None if no city data is found.
        """
        if self.traffic_df.empty:
            return None

        normalized = self._normalize_street_name(street_name)

        # Exact match first
        if normalized in self.street_index:
            return self.street_index[normalized]['avg_count']

        # Partial match - find streets containing this name
        matches = []
        for indexed_street, data in self.street_index.items():
            if normalized in indexed_street or indexed_street in normalized:
                matches.append(data['avg_count'])

        if matches:
            return sum(matches) / len(matches)

        return None

    def _count_to_level(self, count):
        """Map a measured vehicle count to a congestion level name."""
        if count is None:
            return 'unknown'
        if count < self.TRAFFIC_THRESHOLDS['very_low']:
            return 'very_low'
        elif count < self.TRAFFIC_THRESHOLDS['low']:
            return 'low'
        elif count < self.TRAFFIC_THRESHOLDS['moderate']:
            return 'moderate'
        elif count < self.TRAFFIC_THRESHOLDS['high']:
            return 'high'
        return 'very_high'

    def _speed_to_level(self, speed):
        """Map a posted speed limit to an estimated congestion level name."""
        if speed is None or pd.isna(speed) or speed <= 0:
            return 'moderate'  # unknown speed -> assume baseline
        for threshold, level in self.COUNTY_SPEED_THRESHOLDS:
            if speed >= threshold:
                return level
        return 'very_low'

    def _lookup_city(self, normalized):
        """Look a normalized street name up in the city counts index."""
        if not self.street_index:
            return None

        if normalized in self.street_index:
            count = self.street_index[normalized]['avg_count']
        else:
            matches = [data['avg_count'] for street, data in self.street_index.items()
                       if normalized in street or street in normalized]
            if not matches:
                return None
            count = sum(matches) / len(matches)

        level = self._count_to_level(count)
        return {
            'level': level,
            'multiplier': self.CONGESTION_MULTIPLIERS[level],
            'count': count,
            'speed': None,
            'source': 'city_counts'
        }

    def _lookup_county(self, normalized):
        """Look a normalized street name up in the county road index."""
        if not self.county_index:
            return None

        if normalized in self.county_index:
            speed = self.county_index[normalized]['avg_speed']
        else:
            matches = [data['avg_speed'] for street, data in self.county_index.items()
                       if data['avg_speed'] is not None and (normalized in street or street in normalized)]
            if not matches:
                return None
            speed = sum(matches) / len(matches)

        level = self._speed_to_level(speed)
        return {
            'level': level,
            'multiplier': self.CONGESTION_MULTIPLIERS[level],
            'count': None,
            'speed': speed,
            'source': 'county_roads'
        }

    def _lookup_street(self, street_name):
        """
        Unified street lookup across both datasets.

        Measured city counts take priority; if the street is not covered by the
        city dataset we fall back to the county road network estimate. Returns a
        dict with level/multiplier/count/speed/source, or None if unmatched.
        """
        if not street_name:
            return None

        normalized = self._normalize_street_name(street_name)

        city = self._lookup_city(normalized)
        if city is not None:
            return city

        return self._lookup_county(normalized)

    def get_traffic_level(self, street_name):
        """
        Get the traffic congestion level for a street across both datasets.
        Returns a tuple of (level_name, multiplier, count).

        `count` is the measured vehicle count when the match comes from the city
        dataset, or None when it comes from the county road estimate.
        """
        result = self._lookup_street(street_name)
        if result is None:
            return ('unknown', 1.0, None)
        return (result['level'], result['multiplier'], result['count'])

    def calculate_route_adjustment(self, route_steps):
        """
        Calculate traffic-based time adjustment for a route.
        
        Args:
            route_steps: List of route steps with instructions
            
        Returns:
            dict with adjustment details
        """
        if not route_steps:
            return {
                'multiplier': 1.0,
                'confidence': 'low',
                'streets_matched': 0,
                'city_matches': 0,
                'county_matches': 0,
                'street_details': []
            }

        multipliers = []
        street_details = []

        for step in route_steps:
            instruction = step.get('instruction', '')
            streets = self._extract_street_from_instruction(instruction)

            for street in streets:
                result = self._lookup_street(street)

                if result and result['level'] != 'unknown':
                    multipliers.append(result['multiplier'])
                    detail = {
                        'street': street,
                        'level': result['level'],
                        'multiplier': result['multiplier'],
                        'source': result['source']
                    }
                    # Surface the underlying signal: measured count for city
                    # matches, estimated speed limit for county matches.
                    if result['source'] == 'city_counts':
                        detail['count'] = result['count']
                    else:
                        detail['speed'] = result['speed']
                    street_details.append(detail)

        if not multipliers:
            return {
                'multiplier': 1.0,
                'confidence': 'low',
                'streets_matched': 0,
                'city_matches': 0,
                'county_matches': 0,
                'street_details': []
            }

        # Calculate weighted average multiplier
        avg_multiplier = sum(multipliers) / len(multipliers)

        # Determine confidence based on how many streets we matched
        if len(multipliers) >= 5:
            confidence = 'high'
        elif len(multipliers) >= 2:
            confidence = 'medium'
        else:
            confidence = 'low'

        city_matches = sum(1 for d in street_details if d['source'] == 'city_counts')
        county_matches = sum(1 for d in street_details if d['source'] == 'county_roads')

        return {
            'multiplier': round(avg_multiplier, 3),
            'confidence': confidence,
            'streets_matched': len(multipliers),
            'city_matches': city_matches,
            'county_matches': county_matches,
            'street_details': street_details
        }

    def search_streets(self, query, limit=10):
        """
        Search for streets matching a query across both datasets.
        Useful for autocomplete or debugging. City matches (with measured
        counts) are listed first, followed by county road matches.
        """
        if not query:
            return []

        query = query.upper()
        matches = []

        # City matches (measured counts)
        for street, data in self.street_index.items():
            if query in street:
                level = self._count_to_level(data['avg_count'])
                matches.append({
                    'street_name': street,
                    'source': 'city_counts',
                    'avg_count': int(round(data['avg_count'], 0)),
                    'traffic_level': level,
                    'sample_size': int(data['sample_size'])
                })

        matches.sort(key=lambda x: (not x['street_name'].startswith(query), -x['avg_count']))

        # County matches (estimated from speed) — only if we still have room,
        # and skip names already returned from the city dataset.
        if len(matches) < limit:
            seen = {m['street_name'] for m in matches}
            county_matches = []
            for street, data in self.county_index.items():
                if query in street and street not in seen:
                    level = self._speed_to_level(data['avg_speed'])
                    county_matches.append({
                        'street_name': street,
                        'source': 'county_roads',
                        'avg_speed': None if data['avg_speed'] is None else int(round(data['avg_speed'], 0)),
                        'traffic_level': level,
                        'sample_size': int(data['sample_size'])
                    })
            county_matches.sort(key=lambda x: (not x['street_name'].startswith(query), x['street_name']))
            matches.extend(county_matches[:limit - len(matches)])

        return matches[:limit]

    def get_stats(self):
        """Get overall statistics about both traffic datasets."""
        if self.traffic_df.empty and self.county_df.empty:
            return {'status': 'no_data'}

        stats = {
            'city': {'status': 'no_data'},
            'county': {'status': 'no_data'}
        }

        if not self.traffic_df.empty:
            stats['city'] = {
                'total_records': int(len(self.traffic_df)),
                'unique_streets': int(len(self.street_index)),
                'avg_traffic_count': int(round(self.traffic_df['total_count'].mean(), 0)),
                'max_traffic_count': int(round(self.traffic_df['total_count'].max(), 0)),
                'min_traffic_count': int(round(self.traffic_df['total_count'].min(), 0)),
                'date_range': {
                    'earliest': str(self.traffic_df['date_count'].min()) if 'date_count' in self.traffic_df.columns else None,
                    'latest': str(self.traffic_df['date_count'].max()) if 'date_count' in self.traffic_df.columns else None
                }
            }

        if not self.county_df.empty:
            stats['county'] = {
                'total_records': int(len(self.county_df)),
                'unique_streets': int(len(self.county_index)),
                'avg_speed_limit': int(round(self.county_df['SPEED'].mean(skipna=True), 0)) if 'SPEED' in self.county_df.columns else None
            }

        # Combined coverage so callers can see the whole-county reach
        stats['total_unique_streets'] = int(len(self.street_index) + len(self.county_index))

        return stats


# Singleton instance for reuse across the application
traffic_data_instance = TrafficData()


# ============ API Resources ============

class _GetTrafficLevel(Resource):
    """Get traffic level for a specific street."""
    def get(self):
        street = request.args.get('street', '')
        if not street:
            return {'error': 'Street parameter is required'}, 400
        
        level, multiplier, count = traffic_data_instance.get_traffic_level(street)
        return {
            'street': street,
            'traffic_level': level,
            'congestion_multiplier': multiplier,
            'vehicle_count': count
        }, 200


class _SearchStreets(Resource):
    """Search for streets in the traffic database."""
    def get(self):
        query = request.args.get('q', '')
        limit = request.args.get('limit', 10, type=int)
        
        if not query:
            return {'error': 'Query parameter q is required'}, 400
        
        results = traffic_data_instance.search_streets(query, limit)
        return {'results': results, 'count': len(results)}, 200


class _TrafficStats(Resource):
    """Get traffic data statistics."""
    def get(self):
        stats = traffic_data_instance.get_stats()
        return stats, 200


# Register API endpoints
api.add_resource(_GetTrafficLevel, '/traffic/level')
api.add_resource(_SearchStreets, '/traffic/search')
api.add_resource(_TrafficStats, '/traffic/stats')


# ============ Helper Functions for Route Integration ============

def get_average_speed(street_name):
    """Legacy function - returns traffic count (higher = more traffic)."""
    return traffic_data_instance.get_traffic_count(street_name)


def get_traffic_level(street_name):
    """Get traffic level tuple for a street."""
    return traffic_data_instance.get_traffic_level(street_name)


def calculate_route_adjustment(route_steps):
    """Calculate traffic adjustment for a route."""
    return traffic_data_instance.calculate_route_adjustment(route_steps)










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
    Traffic data handler using San Diego traffic counts dataset.
    
    The dataset contains historical traffic counts for streets in San Diego.
    We use total_count to estimate traffic congestion levels.
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

    def __init__(self):
        csv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'traffic_counts_datasd.csv'))
        self.traffic_df = self._load_data(csv_path)
        self._build_street_index()

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

    def _normalize_street_name(self, street_name):
        """
        Normalize street names for better matching.
        Handles common abbreviations and variations.
        """
        if not street_name:
            return ""
            
        name = street_name.upper().strip()
        
        # Common abbreviations mapping
        abbreviations = {
            'STREET': 'ST',
            'AVENUE': 'AV',
            'BOULEVARD': 'BL',
            'DRIVE': 'DR',
            'ROAD': 'RD',
            'LANE': 'LN',
            'COURT': 'CT',
            'PLACE': 'PL',
            'HIGHWAY': 'HW',
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
        Get the average traffic count for a street.
        Returns None if no data is found.
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

    def get_traffic_level(self, street_name):
        """
        Get the traffic congestion level for a street.
        Returns a tuple of (level_name, multiplier, count).
        """
        count = self.get_traffic_count(street_name)
        
        if count is None:
            return ('unknown', 1.0, None)
        
        # Determine traffic level based on thresholds
        if count < self.TRAFFIC_THRESHOLDS['very_low']:
            level = 'very_low'
        elif count < self.TRAFFIC_THRESHOLDS['low']:
            level = 'low'
        elif count < self.TRAFFIC_THRESHOLDS['moderate']:
            level = 'moderate'
        elif count < self.TRAFFIC_THRESHOLDS['high']:
            level = 'high'
        else:
            level = 'very_high'
        
        return (level, self.CONGESTION_MULTIPLIERS[level], count)

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
                'street_details': []
            }
        
        multipliers = []
        street_details = []
        
        for step in route_steps:
            instruction = step.get('instruction', '')
            streets = self._extract_street_from_instruction(instruction)
            
            for street in streets:
                level, mult, count = self.get_traffic_level(street)
                
                if level != 'unknown':
                    multipliers.append(mult)
                    street_details.append({
                        'street': street,
                        'level': level,
                        'multiplier': mult,
                        'count': count
                    })
        
        if not multipliers:
            return {
                'multiplier': 1.0,
                'confidence': 'low',
                'streets_matched': 0,
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
        
        return {
            'multiplier': round(avg_multiplier, 3),
            'confidence': confidence,
            'streets_matched': len(multipliers),
            'street_details': street_details
        }

    def search_streets(self, query, limit=10):
        """
        Search for streets matching a query.
        Useful for autocomplete or debugging.
        """
        if self.traffic_df.empty or not query:
            return []
        
        query = query.upper()
        matches = []
        
        for street, data in self.street_index.items():
            if query in street:
                level, mult, _ = self.get_traffic_level(street)
                matches.append({
                    'street_name': street,
                    'avg_count': int(round(data['avg_count'], 0)),
                    'traffic_level': level,
                    'sample_size': int(data['sample_size'])
                })
        
        # Sort by relevance (exact match first, then by traffic count)
        matches.sort(key=lambda x: (not x['street_name'].startswith(query), -x['avg_count']))
        
        return matches[:limit]

    def get_stats(self):
        """Get overall statistics about the traffic data."""
        if self.traffic_df.empty:
            return {'status': 'no_data'}
        
        return {
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










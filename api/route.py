import os
from flask import Blueprint, request, g
from flask_restful import Api, Resource
import requests
import re
from .traffic import calculate_route_adjustment, get_traffic_level
from .jwt_authorize import token_required
from model.subscription import RouteUsage

# Blueprint and API init
routes_api = Blueprint('routes', __name__, url_prefix='')
api = Api(routes_api)

# Google Maps API key is loaded from the environment (see .env / .env.example).
# Never hardcode credentials in source.
API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY')


def get_user_tier(user):
    """
    Get subscription tier for a user.
    Admins automatically get 'admin' tier with full access.
    """
    from model.subscription import Subscription
    from datetime import datetime
    
    # Admin users get full access
    if hasattr(user, 'role') and user.role == 'Admin':
        return 'admin'
    
    # Check subscription
    subscription = Subscription.query.filter_by(_user_id=user.id).first()
    
    if not subscription:
        return 'free'
    
    # Check if subscription is active and not expired
    if subscription.status != 'active':
        return 'free'
    
    if subscription.expires_at and subscription.expires_at < datetime.utcnow():
        return 'free'
    
    return subscription.tier or 'free'


def strip_html(text):
    """Remove HTML tags from Google Maps instructions."""
    return re.sub(r'<[^>]*>', '', text)


def format_duration(minutes):
    """Format duration in minutes to human-readable string."""
    if minutes < 60:
        return f"{minutes:.0f} mins"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    if mins == 0:
        return f"{hours} hr"
    return f"{hours} hr {mins} mins"


class RoutesAPI:
    class _GetRoutes(Resource):
        @token_required()
        def post(self):
            try:
                user = g.current_user
                tier = get_user_tier(user)
                
                # Check if user can use a route
                can_use, usage_info = RouteUsage.check_can_use_route(user.id, tier)
                
                if not can_use:
                    return {
                        'error': 'Daily route limit reached',
                        'message': 'You have used all your routes for today.',
                        'limit': usage_info['limit'],
                        'used': usage_info['used'],
                        'tier': tier,
                        'upgrade_message': 'Upgrade to Plus for 50 routes/day or Pro for unlimited routes.',
                        'upgrade_url': '/subscription'
                    }, 429
                
                if not API_KEY:
                    return {'error': 'Routing service is not configured'}, 503

                data = request.get_json(silent=True) or {}
                origin = data.get('origin')
                destination = data.get('destination')
                mode = data.get('mode', 'driving')
                include_traffic_details = data.get('include_traffic_details', False)

                # Request validation at the edge: types, lengths, allowed values.
                if not isinstance(origin, str) or not isinstance(destination, str):
                    return {'error': 'Origin and destination are required'}, 400
                origin = origin.strip()
                destination = destination.strip()
                if not origin or not destination:
                    return {'error': 'Origin and destination are required'}, 400
                if len(origin) > 200 or len(destination) > 200:
                    return {'error': 'Origin/destination too long'}, 400
                if mode not in ('driving', 'walking', 'bicycling', 'transit'):
                    return {'error': 'Invalid travel mode'}, 400

                # Pass user input as request params (requests handles URL-encoding),
                # which prevents query/parameter injection into the Google API call.
                params = {
                    'origin': origin,
                    'destination': destination,
                    'alternatives': 'true',
                    'mode': mode,
                    'departure_time': 'now',
                    'key': API_KEY,
                }
                response = requests.get(
                    "https://maps.googleapis.com/maps/api/directions/json",
                    params=params,
                    timeout=10,
                )
                directions_data = response.json()

                # Handle Google API errors with appropriate status codes
                status = directions_data.get('status', 'Unknown error')
                if status != 'OK':
                    # ZERO_RESULTS is not a server error, it's a "no routes found" situation
                    if status == 'ZERO_RESULTS':
                        return {'error': 'No routes found between these locations'}, 404
                    elif status == 'NOT_FOUND':
                        return {'error': 'One or more locations could not be found'}, 404
                    elif status == 'INVALID_REQUEST':
                        return {'error': 'Invalid request - check origin and destination'}, 400
                    else:
                        return {'error': f'Google Maps error: {status}'}, 500

                routes = directions_data['routes']
                route_info = []

                for route in routes:
                    leg = route['legs'][0]
                    steps = leg['steps']
                    route_details = []
                    total_duration_sec = 0

                    for step in steps:
                        instruction = strip_html(step['html_instructions'])
                        distance = step['distance']['text']
                        duration = step['duration']['text']
                        duration_val = step['duration']['value']
                        total_duration_sec += duration_val

                        route_details.append({
                            'instruction': instruction,
                            'distance': distance,
                            'duration': duration,
                            'duration_seconds': duration_val
                        })

                    # Calculate traffic-based adjustment using our dataset
                    traffic_adjustment = calculate_route_adjustment(route_details)
                    
                    # Base duration from Google
                    base_duration_min = total_duration_sec / 60
                    
                    # Apply our traffic multiplier
                    adjusted_duration_min = base_duration_min * traffic_adjustment['multiplier']
                    
                    # Also check if Google provided duration_in_traffic
                    google_traffic_duration = None
                    if 'duration_in_traffic' in leg:
                        google_traffic_duration = leg['duration_in_traffic']['value'] / 60

                    # Build route response
                    route_data = {
                        'details': route_details,
                        'total_duration': leg['duration']['text'],
                        'total_duration_seconds': leg['duration']['value'],
                        'total_distance': leg['distance']['text'],
                        'geometry': route['overview_polyline']['points'],
                        
                        # Traffic-adjusted duration based on SD traffic data
                        'traffic_adjusted_duration': format_duration(adjusted_duration_min),
                        'traffic_adjusted_seconds': int(adjusted_duration_min * 60),
                        
                        # Traffic analysis metadata
                        'traffic_analysis': {
                            'multiplier': traffic_adjustment['multiplier'],
                            'confidence': traffic_adjustment['confidence'],
                            'streets_analyzed': traffic_adjustment['streets_matched'],
                            # Breakdown of which dataset each matched street came
                            # from: city = measured counts, county = speed-based
                            # estimate (extends coverage to the whole county).
                            'city_streets_matched': traffic_adjustment.get('city_matches', 0),
                            'county_streets_matched': traffic_adjustment.get('county_matches', 0)
                        }
                    }
                    
                    # Optionally include detailed street-by-street traffic info
                    if include_traffic_details:
                        route_data['traffic_analysis']['street_details'] = traffic_adjustment['street_details']
                    
                    # Include Google's traffic estimate if available
                    if google_traffic_duration:
                        route_data['google_traffic_duration'] = format_duration(google_traffic_duration)
                        route_data['google_traffic_seconds'] = int(google_traffic_duration * 60)

                    route_info.append(route_data)

                # Increment route usage count after successful route calculation
                usage = RouteUsage.get_today_usage(user.id)
                usage.increment()

                return route_info, 200

            except Exception as e:
                import traceback
                # Log full detail server-side, return a generic message to clients
                # so internal errors are not disclosed.
                print(f"Route API Error: {str(e)}")
                print(traceback.format_exc())
                return {'error': 'Failed to calculate route'}, 500


    class _GetTrafficForStreet(Resource):
        """Get traffic information for a specific street."""
        def get(self):
            street = request.args.get('street', '')
            if not street:
                return {'error': 'Street parameter required'}, 400
            
            level, multiplier, count = get_traffic_level(street)
            return {
                'street': street,
                'traffic_level': level,
                'congestion_multiplier': multiplier,
                'daily_vehicle_count': count
            }, 200


    # Route registration
    api.add_resource(_GetRoutes, '/get_routes')
    api.add_resource(_GetTrafficForStreet, '/street_traffic')








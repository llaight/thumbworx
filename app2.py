from flask import Flask, render_template_string, request, jsonify, render_template
import joblib
import osmnx as ox
import networkx as nx
from shapely.geometry import Point, Polygon
from util import get_eta_minutes, haversine
import psycopg2
import pytz
from datetime import datetime
from dotenv import load_dotenv
import os
from geopy.geocoders import OpenCage
from haversine import haversine
import folium
import requests
import json
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

model = joblib.load('model/delivery_eta_lr.pkl')

load_dotenv()

def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

# city graph for routing
print("Loading city graph...")
try:
    place = "Makati, Metro Manila, Philippines"
    G = ox.graph_from_place(place, network_type="drive")
except Exception as e:
    print("Fallback to bbox due to error:", e)
    north, south, east, west = 14.569, 14.535, 121.043, 121.008
    G = ox.graph_from_bbox(north, south, east, west, network_type="drive")

# helper function
# getting the geocode latitude and longitude of the address
geofence_api_key = os.getenv("GEO_API_KEY")
geolocator = OpenCage(api_key=geofence_api_key)

def get_lat_lng_from_address(address):
    try:
        location = geolocator.geocode(address)
        if location:
            return location.latitude, location.longitude
        else:
            return None, None
    except Exception as e:
        print(f"Geocoding error: {e}")
        return None, None
    
# logging activity
def log_activity(activity_type, details):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
                       INSERT INTO activity_logs(activity_type, details, timestamp)
                       VALUES (%s, %s, %s)
                       """, (activity_type, details, datetime.now()))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Logging error: {e}")

# check geofence
def check_geofence(lat, lng):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, boundary_coordinates FROM geofences")
        geofences = cursor.fetchall()
        cursor.close()
        conn.close()
        
        for name, boundary_text in geofences:
            # Safe evaluation of polygon coordinates
            try:
                # Assuming boundary_coordinates is stored as string representation of list
                poly_points = eval(boundary_text)  # Consider using json.loads for safer parsing
                # Convert to (lng, lat) for Shapely if needed
                poly = Polygon([(point[1], point[0]) for point in poly_points])
                point = Point(lng, lat)
                if poly.contains(point):
                    return name
            except Exception as e:
                print(f"Error parsing geofence {name}: {e}")
                continue
        return None
    except Exception as e:
        print(f"Geofence check error: {e}")
        return None

def suggest_route(origin, destination):
    try:
        origin_node = ox.nearest_nodes(G, origin[1], origin[0])
        dest_node = ox.nearest_nodes(G, destination[1], destination[0])
        nodes = nx.shortest_path(G, origin_node, dest_node, weight='length')
        coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in nodes]
        distance_m = sum(ox.utils_graph.get_route_edge_attributes(G, nodes, 'length'))
        eta_min = distance_m / 500  # ~30km/h average speed
        return coords, round(eta_min, 1)
    except Exception as e:
        print(f"Route calculation error: {e}")
        return [], 0

def assign_driver_to_order(delivery_id, pickup_lat, pickup_lng):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT driver_id, name, current_lat, current_lng, current_load FROM drivers WHERE availability = TRUE")
        drivers = cursor.fetchall()
        
        if not drivers:
            cursor.close()
            conn.close()
            return None
            
        # Find closest driver considering both distance and current load
        best_driver = min(drivers, key=lambda d: haversine((d[2], d[3]), (pickup_lat, pickup_lng)) + (d[4] * 2))  # Weight load more
        driver_id = best_driver[0]
        
        # increment load
        cursor.execute("UPDATE drivers SET current_load = current_load + 1, availability = FALSE WHERE driver_id=%s", (driver_id,))
        # assign driver to delivery
        cursor.execute("UPDATE deliveries SET assigned_driver_id=%s, status='assigned', updated_at=%s WHERE delivery_id=%s", (driver_id, datetime.now(), delivery_id))
        conn.commit()
        cursor.close()
        conn.close()
        
        log_activity("assign_driver", f"Assigned delivery {delivery_id} to driver {best_driver[1]}")
        return {
            "driver_id": driver_id, 
            "driver_name": best_driver[1], 
            "driver_lat": best_driver[2], 
            "driver_lng": best_driver[3]
        }
    except Exception as e:
        print(f"Driver assignment error: {e}")
        return None

# MAP
def plot_map():
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # get drivers
        cur.execute("SELECT name, current_lat, current_lng, availability FROM drivers")
        drivers = cur.fetchall()
        
        # get deliveries
        cur.execute("""
            SELECT d.delivery_id, d.pickup_lat, d.pickup_lng, d.dropoff_lat, d.dropoff_lng, 
                   d.assigned_driver_id, d.status, dr.name
            FROM deliveries d
            LEFT JOIN drivers dr ON d.assigned_driver_id = dr.driver_id
        """)
        deliveries = cur.fetchall()
        
        # get geofences
        cur.execute("SELECT name, boundary_coordinates FROM geofences")
        geofences = cur.fetchall()
        
        cur.close()
        conn.close()

        m = folium.Map(location=[14.5547, 121.0244], zoom_start=13)
        
        # Drivers
        for d in drivers:
            color = 'blue' if d[3] else 'gray'  # Available vs unavailable
            folium.Marker(
                [d[1], d[2]], 
                popup=f"{d[0]} - {'Available' if d[3] else 'Busy'}", 
                icon=folium.Icon(color=color, icon='car', prefix='fa')
            ).add_to(m)
            
        # Deliveries
        for d in deliveries:
            status = d[6] if d[6] else 'unassigned'
            driver_name = d[7] if d[7] else 'Unassigned'
            color = {
                'assigned': 'green',
                'in_transit': 'orange', 
                'delivered': 'purple',
                'unassigned': 'red'
            }.get(status, 'red')
            
            # Pickup location
            folium.Marker(
                [d[1], d[2]], 
                popup=f"Pickup - Delivery {d[0]} ({status})\nDriver: {driver_name}", 
                icon=folium.Icon(color=color, icon='play')
            ).add_to(m)
            
            color_2 = {
                'assigned': 'orange',
                'delivered': 'purple'
            }.get(status, 'orange')
            
            # Dropoff location
            folium.Marker(
                [d[3], d[4]], 
                popup=f"Dropoff - Delivery {d[0]} ({status})\nDriver: {driver_name}", 
                icon=folium.Icon(color= color_2, icon='stop')
            ).add_to(m)
            
        # Geofences
        for gf in geofences:
            try:
                poly_points = eval(gf[1])
                folium.Polygon(
                    poly_points, 
                    color='red', 
                    fill=True, 
                    fill_opacity=0.3, 
                    popup=f"Restricted Zone: {gf[0]}"
                ).add_to(m)
            except Exception as e:
                print(f"Error plotting geofence {gf[0]}: {e}")
                
        return m._repr_html_()
    except Exception as e:
        print(f"Map generation error: {e}")
        return "<p>Error generating map</p>"

# API ENDPOINTS

# drivers post and get 
@app.route('/drivers/add', methods=['POST'])
def add_driver():
    try:
        data = request.get_json()
        name = data.get('name') 
        current_lat, current_lng = get_lat_lng_from_address(data.get('current_address'))

        if current_lat is None or current_lng is None:
            return jsonify({"error": "Invalid address or geocoding failed"}), 400
        
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
                       INSERT INTO drivers(name, current_lat, current_lng, current_load, availability)
                       VALUES (%s, %s, %s, %s, %s)
                       """, (name, current_lat, current_lng, 0, True))
        conn.commit()
        cursor.close()
        conn.close()
        
        log_activity("add_driver", f"Added driver {name} at {current_lat}, {current_lng}")  
        return jsonify({"message": "Driver added successfully", "driver": {"name": name, "lat": current_lat, "lng": current_lng}}), 201
    except Exception as e:
        return jsonify({"error": f"Failed to add driver: {str(e)}"}), 500

@app.route('/drivers/update', methods=['POST'])
def update_drivers():
    try:
        data = request.get_json()
        driver_id = data.get('driver_id')
        current_lat, current_lng = get_lat_lng_from_address(data.get('current_address'))

        if current_lat is None or current_lng is None:
            return jsonify({"error": "Invalid address or geocoding failed"}), 400

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
                       UPDATE drivers
                       SET current_lat = %s, current_lng = %s
                       WHERE driver_id = %s
                       """, (current_lat, current_lng, driver_id))

        if cursor.rowcount == 0:
            conn.rollback()
            cursor.close()
            conn.close()
            return jsonify({"error": "Driver not found"}), 404
            
        conn.commit()
        cursor.close()
        conn.close()
        
        log_activity("update_driver", f"Updated driver {driver_id} to {current_lat}, {current_lng}")
        return jsonify({"message": "Driver location updated successfully"}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to update driver: {str(e)}"}), 500

@app.route('/drivers/log', methods=['GET'])
def get_drivers():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT driver_id, name, current_lat, current_lng, current_load, availability FROM drivers")
        logs = cursor.fetchall()
        cursor.close()
        conn.close()
        
        # Convert to more readable format
        drivers_list = []
        for log in logs:
            drivers_list.append({
                "driver_id": log[0],
                "name": log[1],
                "current_lat": log[2],
                "current_lng": log[3],
                "current_load": log[4],
                "availability": log[5]
            })
        
        return jsonify({"drivers": drivers_list}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to get drivers: {str(e)}"}), 500

# deliveries request
@app.route('/deliveries/request', methods=['POST'])
def add_deliveries_api():
    try:
        data = request.get_json()
        pickup_address = data.get('pickup_address')
        dropoff_address = data.get('dropoff_address')
        
        pickup_lat, pickup_lng = get_lat_lng_from_address(pickup_address)
        dropoff_lat, dropoff_lng = get_lat_lng_from_address(dropoff_address)
        
        if None in [pickup_lat, pickup_lng, dropoff_lat, dropoff_lng]:
            return jsonify({"error": "Invalid pickup or dropoff address"}), 400
        
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
                       INSERT INTO deliveries(pickup_address, pickup_lat, pickup_lng, dropoff_address, dropoff_lat, dropoff_lng, status) 
                       VALUES(%s, %s, %s, %s, %s, %s, %s)
                       RETURNING delivery_id
                       """, (pickup_address, pickup_lat, pickup_lng, dropoff_address, dropoff_lat, dropoff_lng, 'pending'))
        
        result = cursor.fetchone()
        delivery_id = result[0] if result else None
        conn.commit()
        cursor.close()
        conn.close()
        
        log_activity("add_delivery", f"Added delivery {delivery_id} from {pickup_address} to {dropoff_address}")
        return jsonify({
            "message": "Delivery request created successfully", 
            "delivery_id": delivery_id,
            "pickup_coordinates": [pickup_lat, pickup_lng],
            "dropoff_coordinates": [dropoff_lat, dropoff_lng]
        }), 201
    except Exception as e:
        return jsonify({"error": f"Failed to create delivery: {str(e)}"}), 500

@app.route('/deliveries/update', methods=['POST'])
def update_deliveries_api():
    try:
        data = request.get_json()
        delivery_id= data.get('delivery_id')
        new_status = data.get('status')

        if not delivery_id or not new_status:
            return jsonify({'error': "delivery_id and new_status are requires"}), 400
        
        conn= get_connection()
        cursor= conn.cursor()

        cursor.execute("""
                    SELECT assigned_driver_id FROM deliveries WHERE delivery_id = %s
                    """, (delivery_id,))
        result_driver= cursor.fetchone()

        if not result_driver:
            cursor.close()
            conn.close()
            return jsonify({"error": "Delivery not found"}), 404
        
        assigned_driver_id= result_driver[0]

        cursor.execute("""
                       UPDATE deliveries SET status=%s, updated_at=NOW() WHERE delivery_id=%s
                       """, (new_status, delivery_id))
        
        if new_status == 'delivered' and assigned_driver_id:
            cursor.execute("""
                           UPDATE drivers SET availability= TRUE WHERE driver_id=%s
                           """, (assigned_driver_id,))
        
        conn.commit()
        cursor.close()
        conn.close()

        log_activity("update_delivery", f"Updated delivery {delivery_id} to status {new_status}")
        return jsonify({"message": f"Delivery {delivery_id} updated to {new_status}"}), 200
    
    except Exception as e:
        return jsonify({"error": f"Failed to update delivery: {str(e)}"}), 500

@app.route('/deliveries/assign', methods=['GET'])
def assign_deliveries_api():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT delivery_id, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng
            FROM deliveries
            WHERE assigned_driver_id IS NULL AND status='pending'
            """)
    
        deliveries = cursor.fetchall()
        response = []

        for delivery in deliveries:
            delivery_id, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = delivery

            # Check geofences
            if check_geofence(pickup_lat, pickup_lng) or check_geofence(dropoff_lat, dropoff_lng):
                log_activity("geofence_violation", f"Delivery {delivery_id} inside geofence, skipped")
                continue

            # Assign nearest **available** driver
            driver = assign_driver_to_order(delivery_id, pickup_lat, pickup_lng)
            if not driver:
                log_activity("assignment_failed", f"No available driver for delivery {delivery_id}")
                continue

            # ETA calculations
            osmr_url_to_pickup = f"http://router.project-osrm.org/route/v1/driving/{driver['driver_lng']},{driver['driver_lat']};{pickup_lng},{pickup_lat}?overview=full&geometries=geojson"
            osmr_url_to_dropoff = f"http://router.project-osrm.org/route/v1/driving/{pickup_lng},{pickup_lat};{dropoff_lng},{dropoff_lat}?overview=full&geometries=geojson"
            try:
                resp = requests.get(osmr_url_to_pickup, timeout=5).json()
                resp2 = requests.get(osmr_url_to_dropoff, timeout=5).json()
                if resp.get("routes") and resp2.get("routes"):
                    #eta
                    eta_to_pickup = resp["routes"][0]["duration"] / 60
                    eta_delivery = resp2["routes"][0]["duration"] / 60
                    total_eta = eta_to_pickup + eta_delivery

                    #route
                    route_to_pickup = resp["routes"][0]["geometry"]["coordinates"]
                    route_to_dropoff = resp2["routes"][0]["geometry"]["coordinates"]
                    full_route = [f"{lat},{lng}" for lat, lng in route_to_pickup + route_to_dropoff[1:]]

                    #distance
                    distance_to_pickup = resp["routes"][0]["distance"] / 1000  # Convert to km
                    distance_delivery = resp2["routes"][0]["distance"] / 1000  # Convert to km
                    total_distance = distance_to_pickup + distance_delivery
                else:
                    log_activity("route_error", f"OSRM no route for delivery {delivery_id}")
                    continue
            except Exception as e:
                log_activity("route_error", f"OSRM request failed for delivery {delivery_id}: {e}")
                continue
            # eta_to_pickup = float(get_eta_minutes(driver['driver_lat'], driver['driver_lng'], pickup_lat, pickup_lng, model))
            # eta_delivery = float(get_eta_minutes(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, model))
            # total_eta = eta_to_pickup + eta_delivery

            # Route
            # route_to_pickup, _ = suggest_route((driver['driver_lat'], driver['driver_lng']), (pickup_lat, pickup_lng))
            # route_to_dropoff, _ = suggest_route((pickup_lat, pickup_lng), (dropoff_lat, dropoff_lng))
            # full_route = [[float(lat), float(lng)] for lat, lng in route_to_pickup + route_to_dropoff[1:]]


            # Update delivery
            cursor2 = conn.cursor()
            cursor2.execute("""
                UPDATE deliveries
                SET eta_minutes=%s, assigned_driver_id=%s, updated_at=NOW()
                WHERE delivery_id=%s
            """, (total_eta, driver['driver_id'], delivery_id))

            #Update route
            cursor2.execute("""
                            INSERT INTO routes(delivery_id, waypoints, distance_km, duration_minutes)
                            VALUES (%s, %s, %s, %s)
                            """, (delivery_id, full_route, total_distance, total_eta))

            # Set driver unavailable
            cursor2.execute("UPDATE drivers SET availability=False WHERE driver_id=%s", (driver['driver_id'],))
            
            conn.commit()
            cursor2.close()

            response.append({
                 "delivery_id": delivery_id,
                 "pickup_lat": pickup_lat,
                 "pickup_lng": pickup_lng,
                 "dropoff_lat": dropoff_lat,
                 "dropoff_lng": dropoff_lng,
                 "driver_id": driver['driver_id'],
                 "driver_name": driver['driver_name'],
                 "eta_minutes": total_eta,
                 "route_coordinates": full_route
            })

        cursor.close()
        conn.close()
        if request.headers.get('Accept') == 'application/json' or request.is_json:
            return jsonify({"assignments": response, "total_assigned": len(response)})

        log_activity("assigned_delivery", f"Assigned deliveries: {response}")
        # Otherwise return HTML
        map_html = plot_map()
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Delivery Assignments</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
            </style>
        </head>
        <body>
            <h2>Delivery Assignments ({len(response)} total)</h2>
            <h2>Live Map</h2>
            {map_html}
        </body>
        </html>
        """
        return render_template_string(html)

    except Exception as e:
        return jsonify({"error": f"Failed to assign deliveries: {str(e)}"}), 500

@app.route('/deliveries/logs', methods=['GET'])
def get_deliveries_logs():
    conn = get_connection()
    cursor= conn.cursor()
    cursor.execute("""
                   SELECT * FROM deliveries
                   ORDER BY delivery_id DESC""")
    deliveries = cursor.fetchall()
    cursor.close()
    conn.close()

    return jsonify({"deliveries": deliveries}), 200


# Get specific route suggestion
@app.route('/route/suggest', methods=['POST'])
def get_route_suggestion():
    try:
        data = request.get_json()
        origin_addr = data.get('origin_address')
        dest_addr = data.get('destination_address')
        
        origin_lat, origin_lng = get_lat_lng_from_address(origin_addr)
        dest_lat, dest_lng = get_lat_lng_from_address(dest_addr)
        
        if None in [origin_lat, origin_lng, dest_lat, dest_lng]:
            return jsonify({"error": "Invalid origin or destination address"}), 400
            
        route_coords, eta_min = suggest_route((origin_lat, origin_lng), (dest_lat, dest_lng))
        
        # Also get ML model ETA for comparison
        ml_eta = get_eta_minutes(origin_lat, origin_lng, dest_lat, dest_lng, model)
        
        return jsonify({
            "origin": {"address": origin_addr, "coordinates": [origin_lat, origin_lng]},
            "destination": {"address": dest_addr, "coordinates": [dest_lat, dest_lng]},
            "route_coordinates": route_coords,
            "estimated_time_simple": eta_min,
            "estimated_time_ml": ml_eta,
            "distance_points": len(route_coords)
        }), 200
    except Exception as e:
        return jsonify({"error": f"Route calculation failed: {str(e)}"}), 500

# Map endpoint
@app.route('/map', methods=['GET'])
def map_api():
    return plot_map()

# Geofence management endpoints
@app.route('/geofences', methods=['GET'])
def get_geofences():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT geofence_id, name, boundary_coordinates, created_at FROM geofences ORDER BY created_at DESC")
        geofences = cursor.fetchall()
        cursor.close()
        conn.close()
        
        geofences_list = []
        for gf in geofences:
            try:
                coordinates = eval(gf[2]) if gf[2] else []
            except:
                coordinates = []
                
            geofences_list.append({
                "geofence_id": gf[0],
                "name": gf[1],
                "boundary_coordinates": coordinates,
                "created_at": gf[3].isoformat() if gf[3] else None
            })
            
        return jsonify({"geofences": geofences_list, "total": len(geofences_list)}), 200
        
    except Exception as e:
        return jsonify({"error": f"Failed to get geofences: {str(e)}"}), 500

@app.route('/geofences/add', methods=['POST'])
def add_geofence():
    try:
        data = request.get_json()
        name = data.get('name')
        coordinates = data.get('coordinates')  # Expected as list of [lat, lng] pairs
        
        if not name or not coordinates:
            return jsonify({"error": "Name and coordinates are required"}), 400
            
        if len(coordinates) < 3:
            return jsonify({"error": "At least 3 coordinate pairs required for a polygon"}), 400
            
        # Validate coordinate format
        try:
            for coord in coordinates:
                if len(coord) != 2 or not all(isinstance(x, (int, float)) for x in coord):
                    raise ValueError("Invalid coordinate format")
        except:
            return jsonify({"error": "Coordinates must be array of [lat, lng] pairs"}), 400
            
        # Ensure polygon is closed (first point = last point)
        if coordinates[0] != coordinates[-1]:
            coordinates.append(coordinates[0])
            
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO geofences (name, boundary_coordinates)
            VALUES (%s, %s)
            RETURNING geofence_id
        """, (name, str(coordinates)))
        
        result = cursor.fetchone()
        geofence_id = result[0] if result else None
        
        conn.commit()
        cursor.close()
        conn.close()
        
        log_activity("add_geofence", f"Added geofence '{name}' with {len(coordinates)} points")
        
        return jsonify({
            "message": "Geofence added successfully",
            "geofence_id": geofence_id,
            "name": name,
            "coordinate_count": len(coordinates)
        }), 201
        
    except Exception as e:
        return jsonify({"error": f"Failed to add geofence: {str(e)}"}), 500

@app.route('/geofences/<int:geofence_id>', methods=['DELETE'])
def delete_geofence(geofence_id):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Get geofence name before deleting
        cursor.execute("SELECT name FROM geofences WHERE geofence_id = %s", (geofence_id,))
        result = cursor.fetchone()
        
        if not result:
            return jsonify({"error": "Geofence not found"}), 404
            
        geofence_name = result[0]
        
        cursor.execute("DELETE FROM geofences WHERE geofence_id = %s", (geofence_id,))
        conn.commit()
        cursor.close()
        conn.close()
        
        log_activity("delete_geofence", f"Deleted geofence '{geofence_name}' (ID: {geofence_id})")
        
        return jsonify({"message": f"Geofence '{geofence_name}' deleted successfully"}), 200
        
    except Exception as e:
        return jsonify({"error": f"Failed to delete geofence: {str(e)}"}), 500

# Check geofence
@app.route('/geofence/check', methods=['POST'])
def check_geofence_api():
    try:
        data = request.get_json()
        lat, lng = data.get('lat'), data.get('lng')
        
        if lat is None or lng is None:
            return jsonify({"error": "Missing lat or lng parameters"}), 400
            
        gf_name = check_geofence(lat, lng)
        return jsonify({
            "coordinates": [lat, lng],
            "inside_geofence": gf_name is not None, 
            "geofence_name": gf_name,
            "message": f"Location is {'inside' if gf_name else 'outside'} restricted zones"
        })
    except Exception as e:
        return jsonify({"error": f"Geofence check failed: {str(e)}"}), 500

# Activity logs
@app.route('/activity_logs', methods=['GET'])
def get_logs_api():
    try:
        limit = request.args.get('limit', 50, type=int)
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT timestamp, activity_type, details FROM activity_logs ORDER BY timestamp DESC LIMIT %s", (limit,))
        logs = cur.fetchall()
        cur.close()
        conn.close()
        
        logs_list = []
        for log in logs:
            logs_list.append({
                "timestamp": log[0].isoformat() if log[0] else None,
                "activity_type": log[1],
                "details": log[2]
            })
            
        return jsonify({"activity_logs": logs_list})
    except Exception as e:
        return jsonify({"error": f"Failed to get logs: {str(e)}"}), 500

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    try:
        conn = get_connection()
        conn.close()
        return jsonify({"status": "healthy", "database": "connected"}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5001)
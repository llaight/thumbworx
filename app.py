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

from flask_cors import CORS

app = Flask(__name__)

model = joblib.load('model/delivery_eta_lr.pkl')

load_dotenv()

def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port= os.getenv("DB_PORT"),
        database=os.getenv("DB_NAME"),
        user= os.getenv("DB_USER"),
        password= os.getenv("DB_PASSWORD")
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
geofence_api_key= os.getenv("GEO_API_KEY")
geolocator = OpenCage(api_key=geofence_api_key)

def get_lat_lng_from_address(address):
    location = geolocator.geocode(address)
    if location:
        return location.latitude, location.longitude
    else:
        return None, None
    
# logging activity
def log_activity(activity_type, details):
    conn= get_connection()
    cursor= conn.cursor()
    cursor.execute("""
                   INSERT INTO activity_logs(activity_type, details)
                   VALUES (%s, %s)
                   """, (activity_type, details))
    conn.commit()
    cursor.close()
    conn.close()

# check geofence
def check_geofence(lat, lng):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name, polygon FROM geofences")
    for name, polygon_text in cur.fetchall():
        # polygon stored as list of tuples string e.g., '[(lat1,lng1),(lat2,lng2),...]'
        poly_points = eval(polygon_text)
        poly = Polygon(poly_points)
        if poly.contains(Point(lat, lng)):
            cur.close()
            conn.close()
            return name
    cur.close()
    conn.close()
    return None

def suggest_route(origin, destination):
    try:
        origin_node= ox.nearest_nodes(G, origin[1], origin[0])
        dest_node = ox.nearest_nodes(G, destination[1], destination[0])
        nodes= nx.shortest_path(G, origin_node, dest_node, weight='length')
        coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in nodes]
        distance_m = sum(ox.utils_graph.get_route_edge_attributes(G, nodes, 'length'))
        eta_min = distance_m / 500  # ~30km/h
        return coords, round(eta_min, 1)
    except:
        return [], 0

def assign_driver_to_order(delivery_id, pickup_lat, pickup_lng):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT driver_id, name, current_lat, current_lng, current_load FROM drivers")
    drivers = cursor.fetchall()
    best_driver = min(drivers, key=lambda d: haversine((d[2],d[3]), (pickup_lat,pickup_lng)) + d[4])
    driver_id = best_driver[0]
    # increment load
    cursor.execute("UPDATE drivers SET current_load = current_load + 1 WHERE driver_id=%s", (driver_id,))
    # assign driver to delivery
    cursor.execute("UPDATE deliveries SET assigned_driver_id=%s WHERE delivery_id=%s", (driver_id, delivery_id))
    conn.commit()
    cursor.close()
    conn.close()
    log_activity("assign_driver", f"Assigned delivery {delivery_id} to driver {best_driver[1]}")
    return {"driver_id": driver_id, "driver_name": best_driver[1], "driver_lat": best_driver[2], "driver_lng": best_driver[3]}

# MAP
def plot_map():
    conn = get_connection()
    cur = conn.cursor()
    # get drivers
    cur.execute("SELECT name, current_lat, current_lng FROM drivers")
    drivers = cur.fetchall()
    # get deliveries
    cur.execute("SELECT delivery_id, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, assigned_driver_id FROM deliveries")
    deliveries = cur.fetchall()
    # get geofences
    cur.execute("SELECT name, polygon FROM geofences")
    geofences = cur.fetchall()
    cur.close()
    conn.close()

    m = folium.Map(location=[14.5547,121.0244], zoom_start=13)
    # Drivers
    for d in drivers:
        folium.Marker([d[1],d[2]], popup=d[0], icon=folium.Icon(color='blue')).add_to(m)
    # Deliveries
    for d in deliveries:
        color = 'green' if d[5] else 'red'
        folium.Marker([d[2], d[3]], popup=f"Delivery {d[0]}", icon=folium.Icon(color=color)).add_to(m)
        folium.Marker([d[4], d[5]], popup=f"Drop-off {d[0]}", icon=folium.Icon(color='orange')).add_to(m)
    # Geofences
    for gf in geofences:
        poly_points = eval(gf[1])
        folium.Polygon(poly_points, color='red', fill=True, fill_opacity=0.3, popup=gf[0]).add_to(m)
    return m._repr_html_()

# API ENDPOINT

# drivers post and get 
@app.route('/drivers/add', methods=['POST'])
def add_driver():
    data= request.get_json()
    # column
    name= data.get('name') 
    current_lat, current_lng= get_lat_lng_from_address(data.get('current_address'))

    if current_lat is None or current_lng is None:
        return jsonify({"error": "invalid address"}), 400
    
    conn= get_connection()
    cursor= conn.cursor()

    cursor.execute("""
                   INSERT INTO drivers(name, current_lat, current_lng)
                   VALUES (%s, %s, %s)
                   """, (name, current_lat, current_lng))
    conn.commit()
    cursor.close()
    conn.close()
    log_activity("add_driver", f"Added driver {name} at {current_lat}, {current_lng}")  
    return jsonify({"message": "driver added successfully in the database"}), 201

@app.route('/drivers/update', methods=['POST'])
def update_drivers():
    data= request.get_json()
    driver_id= data.get('driver_id')
    current_lat, current_lng= get_lat_lng_from_address(data.get('current_address'))

    if current_lat is None or current_lng is None:
        return jsonify({"error": "invalid address"}), 400

    conn= get_connection()
    cursor= conn.cursor()
    cursor.execute("""
                   UPDATE drivers
                   SET current_lat = %s, current_lng = %s
                   WHERE driver_id = %s
                   """, (current_lat, current_lng, driver_id))
    conn.commit()
    cursor.close()
    conn.close()
    log_activity("update_driver", f"Updated driver {driver_id} to {current_lat}, {current_lng}")
    return jsonify({"message": "driver updated successfully in the database"}), 200

@app.route('/drivers/log', methods=['GET'])
def get_drivers():
    conn= get_connection()
    cursor= conn.cursor()
    cursor.execute("SELECT * FROM drivers")
    logs= cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(logs), 200

# deliveries request
@app.route('/deliveries/request', methods=['POST'])
def add_delivery_api():
    data= request.get_json()
    pickup_address= data.get('pickup_address')
    dropoff_address= data.get('dropoff_address')
    pickup_lat, pickup_lng= get_lat_lng_from_address(pickup_address)
    dropoff_lat, dropoff_lng= get_lat_lng_from_address(dropoff_address)
    if None in [pickup_lat, pickup_lng, dropoff_lat, dropoff_lng]:
        return jsonify({"error": "invalid address"}), 400
    
    conn= get_connection()
    cursor= conn.cursor()

    cursor.execute("""
                   INSERT INTO deliveries(pickup_address, pickup_lat, pickup_lng, dropoff_address, dropoff_lat, dropoff_lng) 
                   VALUES(%s, %s, %s, %s, %s, %s)
                   RETURNING delivery_id
                   """, (pickup_address, pickup_lat, pickup_lng, dropoff_address, dropoff_lat, dropoff_lng))
    result = cursor.fetchone()
    delivery_id = result[0] if result else None
    conn.commit()
    cursor.close()
    conn.close()
    log_activity("add_delivery", f"Added delivery {delivery_id} from {pickup_address} to {dropoff_address}")
    return jsonify({"message": "delivery added successfully in the database", "delivery_id": delivery_id}), 201


@app.route('/deliveries/assign', methods=['GET'])
def assign_deliveries_api():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT delivery_id, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng
        FROM deliveries
        WHERE assigned_driver_id IS NULL
    """)
    deliveries = cursor.fetchall()
    response = []

    for delivery in deliveries:
        delivery_id, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = delivery
        
        # Skip if inside geofence
        # geofence_name = check_geofence(pickup_lat, pickup_lng)
        # if geofence_name:
        #     log_activity("geofence_violation", f"Delivery {delivery_id} inside {geofence_name}, skipped")
        #     continue

        # Assign nearest driver to pickup
        driver = assign_driver_to_order(delivery_id, pickup_lat, pickup_lng)

        # ETA: driver → pickup → dropoff
        eta_to_pickup = get_eta_minutes(
            current_lat=driver['driver_lat'],
            current_lng=driver['driver_lng'],
            dropoff_lat=pickup_lat,
            dropoff_lng=pickup_lng,
            model=model
        )
        eta_delivery = get_eta_minutes(
            current_lat=pickup_lat,
            current_lng=pickup_lng,
            dropoff_lat=dropoff_lat,
            dropoff_lng=dropoff_lng,
            model=model
        )
        total_eta = eta_to_pickup + eta_delivery

        # Route coordinates
        route_to_pickup, _ = suggest_route((driver['driver_lat'], driver['driver_lng']), (pickup_lat, pickup_lng))
        route_to_dropoff, _ = suggest_route((pickup_lat, pickup_lng), (dropoff_lat, dropoff_lng))
        full_route = route_to_pickup + route_to_dropoff

        # Update delivery in DB
        cursor2 = conn.cursor()
        cursor2.execute("""
            UPDATE deliveries 
            SET eta_minutes=%s, assigned_driver_id=%s
            WHERE delivery_id=%s
        """, (total_eta, driver['driver_id'], delivery_id))
        conn.commit()
        cursor2.close()

        # Append to response
        response.append({
            "delivery_id": delivery_id,
            "driver_id": driver['driver_id'],
            "driver_name": driver['driver_name'],
            "eta_min": total_eta,
            "route": full_route
        })

    cursor.close()
    conn.close()

    # Generate map after all assignments
    map_html = plot_map()

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><title>Delivery Assignments</title></head>
    <body>
        <h2>Assignments</h2>
        <pre>{response}</pre>
        <h2>Map</h2>
        {map_html}
    </body>
    </html>
    """
    return render_template_string(html)

# Map endpoint
@app.route('/map', methods=['GET'])
def map_api():
    return plot_map()

# Check geofence
@app.route('/geofence/check', methods=['POST'])
def check_geofence_api():
    data = request.get_json()
    lat, lng = data.get('lat'), data.get('lng')
    gf_name = check_geofence(lat,lng)
    return jsonify({"inside_geofence": gf_name is not None, "geofence_name": gf_name})

# Activity logs
@app.route('/activity_logs', methods=['GET'])
def get_logs_api():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT timestamp, activity_type, details FROM activity_logs ORDER BY timestamp DESC LIMIT 50")
    logs = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(logs)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
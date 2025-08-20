import math 
import datetime
import joblib
import pandas as pd

def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Radius of the Earth in kilometers
    ph1, ph2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2-lat1)
    dlambda = math.radians(lon2-lon1)
    a= math.sin(dphi/2)**2 + math.cos(ph1)*math.cos(ph2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))

# model
model= joblib.load('model/delivery_eta_lr.pkl')

def get_eta_minutes(current_lat, current_lng, dropoff_lat, dropoff_lng, model=model):
    distance= haversine(current_lat, current_lng, dropoff_lat, dropoff_lng)
    now= datetime.datetime.now()
    features = pd.DataFrame([{
        'distance_km': distance,
        'hour': now.hour,
        'weekday': now.weekday()
    }])
    eta_minutes= model.predict(features)[0]
    return max(0.0, float(eta_minutes))  # Ensure non-negative ETA

# driver = {
#     'driver_lat': 14.5547,
#     'driver_lng': 121.0244
# }
# pickup_lat = 14.5995
# pickup_lng = 120.9842
# dropoff_lat = 16.4023
# dropoff_lng = 120.5960

# eta_to_pickup = float(get_eta_minutes(driver['driver_lat'], driver['driver_lng'], pickup_lat, pickup_lng, model))
# eta_delivery = float(get_eta_minutes(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, model))
# total_eta = eta_to_pickup + eta_delivery

# print("ETA to pickup:", eta_to_pickup)
# print("ETA delivery:", eta_delivery)
# print("Total ETA:", total_eta)
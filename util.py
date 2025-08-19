import math 
import datetime
import joblib

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
    features= [[distance, now.hour, now.weekday()]]
    eta_minutes= model.predict(features)[0]
    return max(0.0, float(eta_minutes))  # Ensure non-negative ETA
# 🚚 Smart Logistics Recommendation

This feature provides **efficient route suggestions** and recommends **optimal driver assignments** based on real-time and historical data.  
Its goal is to improve delivery speed, reduce fuel consumption, and balance the workload among drivers.

---

## 🎯 Objectives / Success Criteria

Minimum Viable Products (MVPs) for the Smart Logistics Recommendation team:

- Return a **route suggestion** upon request  
- Recommend an **available driver** based on their current location and load  
- Correctly plot and display the **recommended route** and all relevant points (pick-up, drop-off, driver location) on a map  
- Log all key activities including **route assignment** and **delivery status updates**  
- Successfully process and enforce a minimum of **two predefined geofence boundaries**  
- Provide an **estimated time of arrival (ETA)** 

---

## 🧰 Tech Stack

| Component          | Technology          |
|--------------------|---------------------|
| ML Model           | Python, scikit-learn|
| Routing & ETA      | OSRM (Open Source Routing Machine)       |
| Backend API        | Python, Flask       |
| Database           | PostgreSQL          |
| Frontend           | HTML, Tailwind CSS, JavaScript    |
| Environment Config | python-dotenv       |
| Analytics          | Metabase            |

---

## 🗃️ Project Structure

```plaintext
smart-logistics-recommendation/
├── frontend/              # Frontend dashboard and HTML pages
│   └── dashboard/         # Dashboard HTML, JS, CSS for map and metabase visualization
├── app2.py                # Flask API backend for route suggestion and driver assignment
├── .env                   # Environment variables (DB credentials, API keys, etc.)
└── README.md              # This documentation
```

---

## 📜 License

MIT License — Free to use, modify, and extend for learning or portfolio purposes.

---

## 👩‍💻 Author 
**Angela Loro**  
GitHub: [github.com/llaight](https://github.com/llaight)

**Catherine Joy Paliden**  
GitHub: [github.com/catherinejoy](https://github.com/chickerinejoy)

Developed during internship at LAMINA Studios

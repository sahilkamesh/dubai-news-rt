# Dubai News & Safety Prototype

This is a simple prototype for a real-time UAE safety and news visualization app.

## Backend (FastAPI)
- Provides `/news` and `/areas` endpoints with dummy data.
- Located in `backend/`.

## Frontend (React)
- Displays live news feed and a dummy UAE safety map.
- Located in `frontend/`.

## How to Run

### Backend
1. Open a terminal in `backend/`.
2. Install dependencies:
   ```
pip install -r requirements.txt
   ```
3. Start the server:
   ```
uvicorn main:app --reload
   ```

### Frontend
1. Open a terminal in `frontend/`.
2. Install dependencies:
   ```
npm install
   ```
3. Start the React app:
   ```
npm start
   ```

The frontend will fetch data from the backend at `http://localhost:8000`.

---
This is a prototype using dummy data. Replace with real data sources and map visualization for production use.

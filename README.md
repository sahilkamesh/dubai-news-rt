This is a real-time UAE safety news web app, published at https://uae-live.onrender.com/

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

The app will be available at [http://localhost:3000](http://localhost:3000) in your browser.
The frontend communicates with the backend API running at [http://localhost:8000](http://localhost:8000).

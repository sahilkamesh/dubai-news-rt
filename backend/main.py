from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from pydantic import BaseModel
import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class NewsItem(BaseModel):
    id: int
    timestamp: str
    source: str
    category: str
    location: str
    severity: int
    text: str
    link: str

class AreaStatus(BaseModel):
    area: str
    safetyLevel: str
    lastUpdated: str
    activeAlerts: List[str]

@app.get("/news", response_model=List[NewsItem])
def get_news():
    # Dummy data
    return [
        NewsItem(
            id=1,
            timestamp=str(datetime.datetime.now()),
            source="Reddit",
            category="Missile Sighting",
            location="Dubai Marina",
            severity=3,
            text="Saw a missile in the sky.",
            link="https://reddit.com/r/dubai/attack-thread"
        ),
        NewsItem(
            id=2,
            timestamp=str(datetime.datetime.now()),
            source="The National News",
            category="Shelter Alert",
            location="Abu Dhabi",
            severity=5,
            text="Shelter in place order issued.",
            link="https://thenationalnews.com/uae/conflict-update"
        )
    ]

@app.get("/areas", response_model=List[AreaStatus])
def get_areas():
    # Dummy data
    return [
        AreaStatus(
            area="Dubai Marina",
            safetyLevel="Unsafe",
            lastUpdated=str(datetime.datetime.now()),
            activeAlerts=["Missile sighting"]
        ),
        AreaStatus(
            area="Downtown Dubai",
            safetyLevel="Safe",
            lastUpdated=str(datetime.datetime.now()),
            activeAlerts=[]
        )
    ]

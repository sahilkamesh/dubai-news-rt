import React, { useEffect, useState } from 'react';
import './App.css';

function App() {
  const [news, setNews] = useState([]);
  const [areas, setAreas] = useState([]);

  useEffect(() => {
    fetch('http://localhost:8000/news')
      .then(res => res.json())
      .then(setNews);
    fetch('http://localhost:8000/areas')
      .then(res => res.json())
      .then(setAreas);
  }, []);

  return (
    <div className="App">
      <h1>UAE Safety & News Feed</h1>
      <div className="feed">
        <h2>Live News Feed</h2>
        <ul>
          {news.map(item => (
            <li key={item.id}>
              <b>[{item.severity}] {item.category}</b> - {item.text} <i>({item.source}, {item.location})</i>
            </li>
          ))}
        </ul>
      </div>
      <div className="map">
        <h2>UAE Safety Map (Dummy)</h2>
        <ul>
          {areas.map(area => (
            <li key={area.area} style={{color: area.safetyLevel === 'Unsafe' ? 'red' : 'green'}}>
              {area.area}: {area.safetyLevel} {area.activeAlerts.length > 0 && `(Alerts: ${area.activeAlerts.join(', ')})`}
            </li>
          ))}
        </ul>
        <p style={{fontStyle:'italic'}}>Map visualization placeholder. Replace with interactive map later.</p>
      </div>
    </div>
  );
}

export default App;

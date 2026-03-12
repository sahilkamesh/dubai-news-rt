import React, { useEffect, useState } from 'react';
import './App.css';
import { MapContainer, TileLayer, Polygon, Tooltip } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';

// Dummy UAE area polygons (replace with real geojson for production)
const AREAS = [
  {
    name: 'Dubai Marina',
    coords: [
      [25.0805, 55.1357], [25.0805, 55.1457], [25.0905, 55.1457], [25.0905, 55.1357]
    ]
  },
  {
    name: 'Downtown Dubai',
    coords: [
      [25.1972, 55.2744], [25.1972, 55.2844], [25.2072, 55.2844], [25.2072, 55.2744]
    ]
  }
];

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function severityToColor(sev) {
  // 1 => green, 10 => red (HSL 120 -> 0)
  const s = clamp(Number(sev) || 1, 1, 10);
  const hue = 120 - ((s - 1) / 9) * 120;
  return `hsl(${hue}, 85%, 45%)`;
};

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

  // Map area name to safety info
  const areaStatus = Object.fromEntries(areas.map(a => [a.area, a]));

  return (
    <div className="App">
      <h1>UAE Safety & News Feed</h1>
      <div className="legend">
        <div className="severity-legend">
          <div className="severity-legend-title">Severity</div>
          <div className="severity-legend-row">
            <span className="severity-legend-label">1</span>
            <div className="severity-gradient" aria-label="Severity color scale from green (1) to red (10)" />
            <span className="severity-legend-label">10</span>
          </div>
        </div>
      </div>
      <div className="main-content">
        <div className="feed">
          <h2>Live News Feed</h2>
          <div className="news-list">
            {news.map(item => (
              <div
                className="news-card"
                key={item.id}
                style={{ borderLeftColor: severityToColor(item.severity) }}
              >
                <div className="news-header">
                  <span className="news-category">[{item.category}]</span>
                  <span className="news-source">{item.source}</span>
                  <span className="news-location">{item.location}</span>
                  <span className="news-location">Severity: {clamp(Number(item.severity) || 1, 1, 10)}/10</span>
                  <span className="news-time">{new Date(item.timestamp).toLocaleTimeString()}</span>
                </div>
                <div className="news-body">{item.text}</div>
                <a href={item.link} target="_blank" rel="noopener noreferrer">Source</a>
              </div>
            ))}
          </div>
        </div>
        <div className="map">
          <h2>UAE Severity Map</h2>
          <MapContainer center={[25.2048, 55.2708]} zoom={10} style={{height: '400px', width: '100%'}}>
            <TileLayer
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              attribution="&copy; OpenStreetMap contributors"
            />
            {AREAS.map(area => {
              const status = areaStatus[area.name] || { severity: 1, activeAlerts: [] };
              const sev = clamp(Number(status.severity) || 1, 1, 10);
              const color = severityToColor(sev);
              return (
                <Polygon
                  key={area.name}
                  positions={area.coords}
                  pathOptions={{ color, fillColor: color, fillOpacity: 0.45 }}
                >
                  <Tooltip>
                    <b>{area.name}</b><br/>
                    Severity: {sev}/10<br/>
                    {status.activeAlerts.length > 0 && (
                      <span>Alerts: {status.activeAlerts.join(', ')}</span>
                    )}
                  </Tooltip>
                </Polygon>
              );
            })}
          </MapContainer>
        </div>
      </div>
    </div>
  );
}

export default App;

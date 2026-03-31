import React, { useEffect, useState } from 'react';
import './App.css';
import { MapContainer, TileLayer, Circle, Tooltip } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';

const API_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

// Simple approximate polygons per known area.
// In a real app, replace this with proper GeoJSON.
const AREA_GEOMETRIES = {
  'Dubai Marina': [
    [25.0805, 55.1357], [25.0805, 55.1457], [25.0905, 55.1457], [25.0905, 55.1357]
  ],
  'Downtown Dubai': [
    [25.1972, 55.2744], [25.1972, 55.2844], [25.2072, 55.2844], [25.2072, 55.2744]
  ],
  // Rough boxes for a few additional common areas.
  Satwa: [
    [25.229, 55.280], [25.229, 55.300], [25.243, 55.300], [25.243, 55.280]
  ],
  Karama: [
    [25.240, 55.295], [25.240, 55.320], [25.255, 55.320], [25.255, 55.295]
  ],
  Sharjah: [
    [25.320, 55.360], [25.320, 55.430], [25.410, 55.430], [25.410, 55.360]
  ],
  Ajman: [
    [25.380, 55.430], [25.380, 55.500], [25.470, 55.500], [25.470, 55.430]
  ],
  DIFC: [
    [25.211, 55.273], [25.211, 55.292], [25.233, 55.292], [25.233, 55.273]
  ]
};

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function severityToColor(sev) {
  // 1 => yellow, 10 => red (HSL 60 -> 0)
  const s = clamp(Number(sev) || 1, 1, 10);
  const hue = 60 - ((s - 1) / 9) * 60;
  return `hsl(${hue}, 85%, 45%)`;
};

function App() {
  const [news, setNews] = useState(() => {
    const saved = localStorage.getItem('dubai_news_cache');
    return saved ? JSON.parse(saved) : [];
  });
  const [areas, setAreas] = useState(() => {
    const saved = localStorage.getItem('dubai_areas_cache');
    return saved ? JSON.parse(saved) : [];
  });
  const [expandedDates, setExpandedDates] = useState(() => {
    const today = new Date().toLocaleDateString('en-AE', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      timeZone: 'Asia/Dubai'
    });
    return { [today]: true };
  });
  const [showLow, setShowLow] = useState(false);
  const [showHigh, setShowHigh] = useState(true);

  const toggleDate = (dateStr) => {
    setExpandedDates(prev => ({
      ...prev,
      [dateStr]: !prev[dateStr]
    }));
  };

  useEffect(() => {
    fetch(`${API_URL}/news`)
      .then(res => {
        if (!res.ok) throw new Error('News fetch failed');
        return res.json();
      })
      .then(data => {
        if (data && data.length > 0) {
          setNews(data);
          localStorage.setItem('dubai_news_cache', JSON.stringify(data));
        }
      })
      .catch(err => console.error('Error fetching news:', err));

    fetch(`${API_URL}/areas`)
      .then(res => {
        if (!res.ok) throw new Error('Areas fetch failed');
        return res.json();
      })
      .then(data => {
        if (data && data.length > 0) {
          setAreas(data);
          localStorage.setItem('dubai_areas_cache', JSON.stringify(data));
        }
      })
      .catch(err => console.error('Error fetching areas:', err));
  }, []);

  const filteredNews = Array.isArray(news) ? news.filter(item => {
    const sev = Number(item.severity) || 1;
    if (showLow && sev <= 5) return true;
    if (showHigh && sev >= 6) return true;
    return false;
  }) : [];

  const sortedNews = [...filteredNews].sort(
    (a, b) => new Date(b.timestamp) - new Date(a.timestamp)
  );

  const filteredAreas = Array.isArray(areas) ? areas.filter(area => {
    const sev = Number(area.severity) || 1;
    if (showLow && sev <= 5) return true;
    if (showHigh && sev >= 6) return true;
    return false;
  }) : [];

  // Map area name to safety info
  const areaStatus = Object.fromEntries(filteredAreas.map(a => [a.area, a]));

  const groupedNews = {};
  sortedNews.forEach(item => {
    const dt = new Date(item.timestamp);
    const dateStr = dt.toLocaleDateString('en-AE', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      timeZone: 'Asia/Dubai'
    });
    if (!groupedNews[dateStr]) {
      groupedNews[dateStr] = [];
    }
    groupedNews[dateStr].push(item);
  });

  return (
    <div className="App">
      <h1>UAE Safety News Feed</h1>
      <div className="legend">
        <div className="filter-controls">
          <span className="filter-label">Filter Severity:</span>
          <button 
            className={`toggle-button ${showLow ? 'active low' : ''}`}
            onClick={() => setShowLow(!showLow)}
          >
            Low (0-5)
          </button>
          <button 
            className={`toggle-button ${showHigh ? 'active high' : ''}`}
            onClick={() => setShowHigh(!showHigh)}
          >
            High (6-10)
          </button>
        </div>
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
        <div className="map">
          <h2>UAE Incident Map</h2>
          <MapContainer center={[24.71, 54.74]} zoom={8} style={{ height: '400px', width: '100%' }}>
            <TileLayer
              url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"
              attribution="&copy; OpenStreetMap contributors &copy; CARTO"
            />
            {filteredAreas.map(area => {
              // const geom = AREA_GEOMETRIES[area.area];
              const geom = area.coordinates;
              // console.log(geom)  
              if (!geom || geom.length < 2) {
                return null;
              }
              const sev = clamp(Number(area.severity) || 1, 1, 10);
              const color = severityToColor(sev);

              // Fade older alerts by lowering fill opacity.
              let opacity = 0.2;
              if (area.lastUpdated) {
                const last = new Date(area.lastUpdated);
                const ageMinutes = (Date.now() - last.getTime()) / 60000;
                if (ageMinutes <= 10) {
                  opacity = 0.7;
                } else if (ageMinutes <= 60) {
                  opacity = 0.45;
                } else if (ageMinutes <= 180) {
                  opacity = 0.3;
                } else {
                  opacity = 0.2;
                }
              }

              return (
                <Circle
                  key={area.area}
                  positions={geom}
                  center={geom}
                  radius={1000}
                  pathOptions={{ color, fillColor: color, fillOpacity: opacity }}
                >
                  <Tooltip sticky={true}>
                    <b>{area.area}</b><br />
                    Severity: {sev}/10<br />
                    {area.lastUpdated && (
                      <>
                        Last update: {new Date(area.lastUpdated).toLocaleTimeString('en-AE', { timeZone: 'Asia/Dubai' })}<br />
                      </>
                    )}
                    {area.activeAlerts && area.activeAlerts.length > 0 && (
                      <span>
                        Alerts: {(function() {
                          const text = area.activeAlerts.join(', ');
                          return text.length > 180 ? text.substring(0, 180) + '...' : text;
                        })()}
                      </span>
                    )}
                  </Tooltip>
                </Circle>
              );
            })}
          </MapContainer>
        </div>
        <div className="feed">
          <h2>Live News Feed</h2>
          <div className="news-list">
            {Object.entries(groupedNews).map(([dateStr, items]) => {
              const isExpanded = expandedDates[dateStr];
              return (
                <div key={dateStr} className="date-group">
                  <div className="date-section-header" onClick={() => toggleDate(dateStr)}>
                    <span>{dateStr}</span>
                    <span className="toggle-icon">{isExpanded ? '▼' : '▶'}</span>
                  </div>
                  {isExpanded && <div className="date-section-content">
                    {items.map(item => {
                      const dt = new Date(item.timestamp);
                      return (
                        <div
                          className="news-card"
                          key={item.id}
                          style={{ borderLeftColor: severityToColor(item.severity) }}
                        >
                          <div className="news-header">
                            <span className="news-source">{item.source}</span>
                            <span className="news-location">{item.location}</span>
                            <span className="news-location">Severity: {clamp(Number(item.severity) || 1, 1, 10)}/10</span>
                            <span className="news-time">
                              {dt.toLocaleTimeString('en-AE', { timeZone: 'Asia/Dubai' })}
                            </span>
                          </div>
                          <div className="news-title" style={{ fontWeight: 'bold', marginTop: '8px' }}>{item.incident}</div>
                          <div className="news-body">{item.summary}</div>
                          <a href={item.link} target="_blank" rel="noopener noreferrer">Source</a>
                        </div>
                      );
                    })}
                  </div>}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;

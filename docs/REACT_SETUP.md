# React + Vite Frontend Setup

OptionsLab dashboard is now built with React 18 + Vite. Same design system (dark terminal theme, lime accents), better component architecture, easier to extend during the roadmap.

## Quick Start

### Prerequisites
- Node.js 18+ (install from nodejs.org)
- Python 3.10+ (for the backend)

### Development (with hot reload)

```bash
# Terminal 1: Backend API
cd optionslab
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app.main:app --reload
# → API at http://localhost:8000/docs

# Terminal 2: Frontend dev server
cd frontend
npm install
npm run dev
# → Dashboard at http://localhost:5173
# (proxies /strategies, /portfolio, etc to :8000 automatically)
```

Edit React components in `frontend/src/` and see changes instantly. Backend endpoints in `app/api/strategies.py` also reload on change.

### Production Build

```bash
cd frontend
npm install
npm run build
# Creates app/static/ with optimized React bundle

# Then run the backend
cd ..
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
# → Dashboard at http://localhost:8000/
# (serves the built React app + API endpoints)
```

## File Structure

```
frontend/
  package.json                    # npm config
  vite.config.js                  # Vite build config
  index.html                      # React root HTML (fonts from CDN)
  src/
    main.jsx                      # Entry point
    App.jsx                       # Root component (state, routing)
    styles/
      globals.css                 # Design system (all colors, fonts, spacing)
    components/                   # Reusable UI pieces
      Header.jsx                  # Tape with logo + token chip + clock
      Nav.jsx                     # Top nav (Positions/Activity/Data/History)
      Summary.jsx                 # 4-column metric strip
      StrategyList.jsx            # Left column cards
      Toast.jsx                   # Toast notification
      NewStrategyModal.jsx        # Add strategy dialog
      DeployModal.jsx             # Deploy dialog
      LiveModal.jsx               # Live promotion checklist
    pages/                        # Full-screen views
      PositionsView.jsx           # Portfolio + strategy table
      ActivityView.jsx            # Event timeline
      DataView.jsx                # Data coverage + MCX toggle
      HistoryView.jsx             # All-time trades
      StrategyDetail.jsx          # Single strategy tabs
```

## Design System

All colors are CSS variables in `frontend/src/styles/globals.css`. To change the theme:

```css
:root {
  --bg: #0A0D0B;           /* background */
  --lime: #B8F04A;         /* action accent, success */
  --green: #3DD98F;        /* profit */
  --red: #F0716B;          /* loss */
  --amber: #F0B23E;        /* warning */
  /* ... etc ... */
}
```

Fonts (Google Fonts, loaded in `index.html`):
- **Space Grotesk** — headings, display
- **Inter** — body text
- **IBM Plex Mono** — numbers, code

Edit CSS variables and fonts only touch the CDN; no rebuild needed for theme changes.

## Adding Features (for Roadmap M1–M8)

### Example: Add a new view

1. Create `frontend/src/pages/MyNewView.jsx`:

```jsx
export default function MyNewView() {
  const [data, setData] = useState(null)

  useEffect(() => {
    const load = async () => {
      const d = await fetch('/my/new/endpoint').then(r => r.json())
      setData(d)
    }
    load()
  }, [])

  return (
    <div className="panel-body">
      {/* Your content */}
    </div>
  )
}
```

2. Import it in `App.jsx` and wire it into the router:

```jsx
import MyNewView from './pages/MyNewView'

// Inside renderView():
case 'mynew':
  return <MyNewView />
```

3. Add a nav button in `Nav.jsx`:

```jsx
const views = ['positions', 'activity', 'data', 'history', 'mynew']
const labels = ['Positions', 'Activity', 'Data', 'Trade history', 'My View']
```

That's it. Backend stays separate; you just add a new view that calls your endpoint.

### Example: Wire a new endpoint

Backend: Add the endpoint in `app/api/strategies.py`:

```python
@router.get("/my/new/endpoint")
def my_endpoint():
  return {"status": "ok", "data": [...]}
```

Frontend: Call it from any component:

```jsx
const data = await fetch('/my/new/endpoint').then(r => r.json())
```

If you need a modal, copy `NewStrategyModal.jsx` and adapt.

## Roadmap Integration

For each milestone (M1–M8), the backend adds endpoints and the frontend wires them:

- **M2** (live feed): Backend starts pushing ticks; frontend wakes up the chart component to animate
- **M3** (chain poller): `/strategies/{id}/chain` endpoint returns bid/ask; fills table updates
- **M6** (walk-forward): `/strategies/{id}/walkforward` endpoint + new Lab tab with results table
- **M7** (risk panel): New `pages/RiskPanel.jsx` view; calls `/portfolio/risk`

No need to rewrite the dashboard; just add new components that call new endpoints.

## Notes

- **No TypeScript by default** — plain JS keeps setup minimal. Add it later if you want: `npm install -D typescript`
- **No state management library** — React hooks + context handle it; refactor to Zustand/Redux only if state gets complex
- **No CSS framework** — Vite ships CSS directly; Tailwind is unnecessary when CSS variables do the job
- **No backend mock** — Vite proxies all `/` paths (except /src, /node_modules) to localhost:8000, so real API calls work instantly

## Troubleshooting

**"Module not found" in Vite dev server**
- Make sure backend is running on http://localhost:8000
- Check `vite.config.js` proxy setup

**CSS not updating**
- Hard refresh browser (Shift+F5)
- Check dev tools to confirm CSS variables are changing

**Build fails**
- `npm install` again (node_modules sometimes gets corrupted)
- Delete `frontend/dist/` and try `npm run build` again

**API calls returning 404**
- Ensure backend is running
- Check the endpoint exists in `app/api/strategies.py`
- Use browser DevTools Network tab to see the actual request

## Deployment

On your VPS (per `deploy/SETUP.md`):

1. Build the frontend: `cd frontend && npm run build`
2. Run the backend: `systemctl start optionslab` (uses `app/static/` as served by FastAPI)

The built React app is a single `index.html` + `assets/*.js` + `assets/*.css`. Vite minifies and optimizes everything. Total bundle ~100 KB gzipped.

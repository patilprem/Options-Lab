# Frontend — React + Vite

The dashboard is now a modern React SPA with TypeScript, Tailwind CSS, and Vite for fast dev/prod builds.

## Local development

```bash
# Terminal 1: backend
cd /path/to/optionslab
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# API running at http://localhost:8000/docs

# Terminal 2: frontend
cd ui
npm install
npm run dev
# Dashboard at http://localhost:5173
# (automatically proxied to http://localhost:8000 for API calls)
```

Changes to `.tsx` or `.css` files hot-reload in the browser instantly.

## Production build

```bash
cd ui
npm run build
# Outputs to app/static/dist/
```

Then the backend serves the built React app at `http://localhost:8000/`.

## Project structure

```
ui/
  src/
    api.ts                           # API client + formatters
    index.css                        # Tailwind + dark theme
    main.tsx                         # React entry
    App.tsx                          # Main layout & nav
    components/
      Header.tsx                     # Status tape
      Summary.tsx                    # 4-column grid
      StrategyList.tsx              # Left column
      DetailPanel.tsx               # View router
      NewStrategyModal.tsx          # Paste-code dialog
      views/
        PositionsView.tsx           # Combined portfolio
        ActivityView.tsx            # Event timeline
        DataView.tsx                # Coverage & MCX recorder
        HistoryView.tsx             # Trade blotter
        StrategyDetail.tsx          # Per-strategy tabs (WIP)
  index.html
  vite.config.ts
  tailwind.config.js
  tsconfig.json
  package.json
```

## Design system

Tailwind config at `tailwind.config.js` defines the dark theme:
- **Colors:** bg (#0A0D0B), panel (rgba white), lime (#B8F04A), green, red, amber
- **Fonts:** Space Grotesk (display), Inter (body), IBM Plex Mono (numbers)
- **Components:** `.glass-panel` (glass effect), `.badge-*` (state badges), `.pnl-pos`/`.pnl-neg`

All styling is Tailwind classes; no inline styles.

## Adding new views

1. Create `src/components/views/NewView.tsx`:
   ```tsx
   export default function NewView() {
     return <div className="glass-panel p-6">...</div>;
   }
   ```

2. Import in `DetailPanel.tsx`:
   ```tsx
   import NewView from './views/NewView';
   ```

3. Add route in `DetailPanel`:
   ```tsx
   if (view === 'newview') return <NewView />;
   ```

4. Add nav button in `App.tsx`:
   ```tsx
   {(['positions', 'activity', 'data', 'history', 'newview'] as const).map(...)}
   ```

## Extending the API client

New endpoints? Add to `api.ts`:
```tsx
export async function getMyData() {
  return api<any>('/my/endpoint');
}
```

Then use in components:
```tsx
const data = await getMyData();
```

## Troubleshooting

**Hot reload not working:** Vite needs file changes to trigger. Saving an import or modifying a CSS file should flush the cache.

**API calls failing locally:** Check that the backend is running on `localhost:8000` and that `vite.config.ts` proxy is configured (should be automatic).

**Build succeeds but app shows blank:** Check browser console for errors. If API routes are 404ing, ensure the backend is serving the React app via the SPA fallback in `app/main.py`.

## What's next (roadmap M1–M8)

- **StrategyDetail component:** tabs for Live/Paper/Backtest/Lab/Code (partially stubbed)
- **Calendar heatmap:** day/week/month views with color intensity
- **Monte Carlo UI:** param editor and MC runner in the Lab tab
- **Equity curve chart:** Chart.js candlestick/line with glowing gradient

# OptionsLab Frontend — React + Vite

Single-page app serving the complete trading dashboard. Dark glassy theme with lime accents.

## Development

```bash
# Install dependencies
npm install

# Dev server with hot reload (proxy to backend at :8000)
npm run dev
# → http://localhost:5173

# Build for production
npm run build
# → output goes to ../app/static/
```

## Architecture

- **Vite** — fast dev server + bundler
- **React 18** — components, hooks, state
- **No build dependencies in production** — the built static files are served by FastAPI
- **Styling** — plain CSS in `src/styles/globals.css` (no Tailwind needed; the design system is baked into CSS variables)
- **API calls** — fetch in each component; could move to a context/hook later

## Components

```
src/
  App.jsx                          # Root app, state, routing
  main.jsx                         # Entry point
  styles/
    globals.css                    # Design system, all colors & fonts
  components/
    Header.jsx                     # Tape + token chip
    Nav.jsx                        # Positions / Activity / Data / History nav
    Summary.jsx                    # 4-column metric strip
    StrategyList.jsx               # Left column: strategy cards
    Toast.jsx                      # Toast notification
    NewStrategyModal.jsx           # Add strategy dialog
    DeployModal.jsx                # Deploy dialog
    LiveModal.jsx                  # Live promotion checklist
  pages/
    PositionsView.jsx              # All strategies + portfolio view
    ActivityView.jsx               # Event timeline
    DataView.jsx                   # Data coverage + MCX recording toggle
    HistoryView.jsx                # All-time trade history
    StrategyDetail.jsx             # Single strategy with tabs
```

## Wiring to Backend

API calls use simple fetch; endpoints are proxied during dev. In production, FastAPI serves the static build directly at `/` and all API paths work the same way:

- `/strategies` — strategy list
- `/portfolio/today` — positions view
- `/activity?date=YYYY-MM-DD` — activity timeline
- `/data/coverage` — data manager
- `/trades` — trade history
- `/strategies/{id}` — strategy detail + code
- `/strategies/{id}/metrics` — performance metrics
- `/strategies/{id}/calendar` — calendar P&L
- etc.

## Styling

All colors are CSS variables in `globals.css`:

```css
--bg: #0A0D0B;           /* near-black */
--lime: #B8F04A;         /* action accent */
--green: #3DD98F;        /* profit */
--red: #F0716B;          /* loss */
--amber: #F0B23E;        /* warning */
```

Fonts are Google Fonts loaded in `index.html`:

- Space Grotesk — display
- Inter — body
- IBM Plex Mono — numbers/code

To change the theme, edit these variables; all components inherit them.

## Next Steps (Roadmap Integration)

- **M1–M3:** API endpoints stay the same; frontend just calls them
- **Strategy Lab:** Create a new `pages/StrategyLab.jsx` tab with param editing + Monte Carlo UI
- **Calendar views:** Extend the placeholder in StrategyDetail with actual calendar rendering (month grid or bar charts)
- **Equity curve:** Wire Chart.js canvas into the paper performance tab

## Notes

- **No TypeScript** for now (setup is minimal); add TS later if you want it
- **No state management library** (Zustand, Redux); React context + hooks handle it; could refactor into a custom hook if state gets complex
- **No CSS-in-JS** (styled-components, etc.); plain CSS keeps it simple and fast

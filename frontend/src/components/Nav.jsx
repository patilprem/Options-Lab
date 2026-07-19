import {
  LayoutDashboard, Boxes, ShieldAlert, Activity, Database, Radar, History,
} from 'lucide-react'

const ITEMS = [
  { view: 'positions', label: 'Dashboard', Icon: LayoutDashboard },
  { view: 'strategies', label: 'Strategies', Icon: Boxes },
  { view: 'risk', label: 'Risk', Icon: ShieldAlert },
  { view: 'activity', label: 'Activity', Icon: Activity },
  { view: 'data', label: 'Data', Icon: Database },
  { view: 'scanner', label: 'Scanner', Icon: Radar },
  { view: 'history', label: 'Trade history', short: 'History', Icon: History },
]

export default function Nav({ view, setView, pending, onNavigate }) {
  const go = (v) => {
    onNavigate && onNavigate()   // leave a selected strategy when switching views
    setView(v)
  }

  // a pending adaptive-update proposal puts a dot on its tab, cleared the
  // moment the user applies/dismisses it
  const hasDot = (v) =>
    (v === 'scanner' && !!pending?.scanner) ||
    (v === 'strategies' && (pending?.strategies?.length > 0))

  return (
    <nav className="nav nav-side" aria-label="Main">
      {ITEMS.map(({ view: v, label, short, Icon }) => (
        <button key={v} className={view === v ? 'on' : ''} onClick={() => go(v)}
          style={{ position: 'relative' }}>
          <Icon size={17} strokeWidth={1.75} aria-hidden="true" />
          {/* full label in the desktop sidebar; a short one in the mobile tab bar */}
          <span className="nav-label">{label}</span>
          <span className="nav-label-short">{short || label}</span>
          {hasDot(v) && (
            <span
              title="An update is waiting for your decision"
              aria-label="update available"
              style={{
                position: 'absolute', top: 6, right: 6, width: 8, height: 8,
                borderRadius: '50%', background: 'var(--amber, #e0a83a)',
                boxShadow: '0 0 0 2px var(--bg, #111)',
              }}
            />
          )}
        </button>
      ))}
    </nav>
  )
}

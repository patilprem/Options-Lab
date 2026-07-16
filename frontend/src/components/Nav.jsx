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

export default function Nav({ view, setView, onNavigate }) {
  const go = (v) => {
    onNavigate && onNavigate()   // leave a selected strategy when switching views
    setView(v)
  }

  return (
    <nav className="nav nav-side" aria-label="Main">
      {ITEMS.map(({ view: v, label, short, Icon }) => (
        <button key={v} className={view === v ? 'on' : ''} onClick={() => go(v)}>
          <Icon size={17} strokeWidth={1.75} aria-hidden="true" />
          {/* full label in the desktop sidebar; a short one in the mobile tab bar */}
          <span className="nav-label">{label}</span>
          <span className="nav-label-short">{short || label}</span>
        </button>
      ))}
    </nav>
  )
}

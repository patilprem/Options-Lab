import { useState, useEffect, useCallback } from 'react'
import Header from './components/Header'
import Nav from './components/Nav'
import Summary from './components/Summary'
import StrategiesView from './pages/StrategiesView'
import PositionsView from './pages/PositionsView'
import RiskView from './pages/RiskView'
import ActivityView from './pages/ActivityView'
import DataView from './pages/DataView'
import ScannerView from './pages/ScannerView'
import HistoryView from './pages/HistoryView'
import StrategyDetail from './pages/StrategyDetail'
import NewStrategyModal from './components/NewStrategyModal'
import DeployModal from './components/DeployModal'
import LiveModal from './components/LiveModal'
import Toast from './components/Toast'

const TITLES = {
  positions: 'Dashboard', strategies: 'Strategies', risk: 'Risk',
  activity: 'Activity', data: 'Data', scanner: 'Scanner', history: 'Trade history',
}

const API = {
  async call(path, options = {}) {
    const resp = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText)
    return resp.json()
  }
}

export default function App() {
  const [view, setView] = useState('positions')
  const [selectedId, setSelectedId] = useState(null)
  const [strategies, setStrategies] = useState([])
  const [summary, setSummary] = useState({ alloc: 0, equity: 0, growth: 0, live: 0 })
  const [toast, setToast] = useState(null)
  const [pending, setPending] = useState({ scanner: false, strategies: [] })
  const [newModalOpen, setNewModalOpen] = useState(false)
  const [deployModalOpen, setDeployModalOpen] = useState(false)
  const [liveModalOpen, setLiveModalOpen] = useState(false)
  const [deployTarget, setDeployTarget] = useState(null)

  const showToast = useCallback(msg => {
    setToast(msg)
    setTimeout(() => setToast(null), 2600)
  }, [])

  // Which nav tabs carry a pending adaptive-update dot. Polled on a loop and
  // refreshed immediately after the user acts on a proposal, so the dot
  // vanishes the moment a decision is taken.
  const refreshPending = useCallback(async () => {
    try { setPending(await API.call('/adaptation/pending')) } catch (e) { /* dots degrade gracefully */ }
  }, [])

  const loadList = useCallback(async () => {
    try {
      const data = await API.call('/strategies')
      setStrategies(data)
      let alloc = 0, live = 0
      data.forEach(s => {
        alloc += s.allocated_capital || 0
        if (s.state === 'RUNNING') live++
      })
      let equity = 0, growth = 0
      try {
        const pf = await API.call('/portfolio/today')
        equity = pf.totals?.equity ?? 0
        growth = pf.totals?.growth ?? 0
      } catch (e) { /* summary cards degrade gracefully */ }
      setSummary({ alloc, equity, growth, live })
    } catch (e) {
      showToast('Failed to load strategies')
    }
  }, [showToast])

  useEffect(() => {
    loadList()
    refreshPending()
    const iv = setInterval(() => { loadList(); refreshPending() }, 15000)
    return () => clearInterval(iv)
  }, [loadList, refreshPending])

  // Detail is driven purely by `selectedId`, so `view` keeps remembering where
  // you came from (Dashboard or Strategies) and Back returns you there.
  const goStrategy = (id) => setSelectedId(id)

  const handleNewStrategy = async (name, code) => {
    try {
      await API.call('/strategies', {
        method: 'POST',
        body: JSON.stringify({ name, code })
      })
      setNewModalOpen(false)
      showToast('Strategy validated ✓')
      await loadList()
    } catch (e) {
      throw e
    }
  }

  const handleDeploy = async (capital, sqOff, start) => {
    try {
      await API.call(`/strategies/${deployTarget}/deploy`, {
        method: 'POST',
        body: JSON.stringify({
          capital,
          square_off_on_pause: sqOff,
          start_immediately: start
        })
      })
      setDeployModalOpen(false)
      showToast(start ? 'Deployed & running ▶' : 'Deployed — paused')
      await loadList()
    } catch (e) {
      throw e
    }
  }

  const renderView = () => {
    if (selectedId) {
      return (
        <StrategyDetail
          id={selectedId}
          onBack={() => setSelectedId(null)}
          onDeploy={(id) => {
            setDeployTarget(id)
            setDeployModalOpen(true)
          }}
          onLive={(id) => {
            setDeployTarget(id)
            setLiveModalOpen(true)
          }}
          onChanged={loadList}
          onAdaptDecision={refreshPending}
          showToast={showToast}
        />
      )
    }

    switch (view) {
      case 'positions':
        return <PositionsView onStrategyClick={goStrategy} />
      case 'strategies':
        return (
          <StrategiesView
            strategies={strategies}
            onSelect={goStrategy}
            onNew={() => setNewModalOpen(true)}
            onDeleted={loadList}
            showToast={showToast}
          />
        )
      case 'risk':
        return <RiskView showToast={showToast} />
      case 'activity':
        return <ActivityView />
      case 'data':
        return <DataView showToast={showToast} />
      case 'scanner':
        return <ScannerView showToast={showToast} onDecision={refreshPending} />
      case 'history':
        return <HistoryView strategies={strategies} />
      default:
        return <PositionsView onStrategyClick={goStrategy} />
    }
  }

  const selected = strategies.find(s => s.id === selectedId)
  const title = selectedId ? (selected?.name || 'Strategy') : TITLES[view]

  return (
    <div className="app">
      <aside className="side">
        <div className="brand">
          <img src="/favicon-96x96.png" alt="" className="brand-mark" />
          <span>OPTIONS<em>LAB</em></span>
        </div>
        <div className="side-label">Menu</div>
        {/* Nav only — the strategy list lives on its own page so it scales
            past a handful of strategies. */}
        <Nav view={view} setView={setView} pending={pending} onNavigate={() => setSelectedId(null)} />
      </aside>

      <main className="content">
        <Header title={title} showToast={showToast} />
        <Summary {...summary} />
        <div className="panel">{renderView()}</div>
      </main>

      <NewStrategyModal
        open={newModalOpen}
        onClose={() => setNewModalOpen(false)}
        onSave={handleNewStrategy}
        showToast={showToast}
      />
      <DeployModal
        open={deployModalOpen}
        onClose={() => setDeployModalOpen(false)}
        onDeploy={handleDeploy}
        showToast={showToast}
      />
      <LiveModal
        open={liveModalOpen}
        id={deployTarget}
        showToast={showToast}
        onClose={() => setLiveModalOpen(false)}
      />
      {toast && <Toast message={toast} />}
    </div>
  )
}

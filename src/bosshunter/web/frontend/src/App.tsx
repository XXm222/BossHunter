import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Sidebar } from './components/layout/Sidebar'
import { Header } from './components/layout/Header'
const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const ConfigPage = lazy(() => import('./pages/ConfigPage'))

const RecruitingPage = lazy(() => import('./pages/RecruitingPage'))

function JobsPage() {
  return <DashboardPage view="jobs" />
}

function MonitorPage() {
  return <DashboardPage view="monitor" />
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen overflow-hidden bg-slate-50 text-slate-900">
        <Sidebar />
        <div className="min-w-0 flex-1 flex flex-col overflow-hidden">
          <Header />
          <main className="min-w-0 flex-1 overflow-y-auto p-3 md:p-6">
            <Suspense fallback={<div role="status">正在载入招聘工作台…</div>}><Routes>
              <Route path="/" element={<Navigate to="/recruiting" replace />} />
              <Route path="/recruiting" element={<RecruitingPage />} />
              <Route path="/recruiting/settings" element={<ConfigPage aiOnly />} />
              <Route path="/recruiting/:section" element={<RecruitingPage />} />
              <Route path="/recruiting/candidates/:id" element={<RecruitingPage />} />
              <Route path="/jobseeker" element={<DashboardPage />} />
              <Route path="/jobs" element={<Navigate to="/recruiting" replace />} />
              <Route path="/jobseeker/jobs" element={<JobsPage />} />
              <Route path="/monitor" element={<MonitorPage />} />
              <Route path="/config" element={<ConfigPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes></Suspense>
          </main>
        </div>
      </div>
    </BrowserRouter>
  )
}

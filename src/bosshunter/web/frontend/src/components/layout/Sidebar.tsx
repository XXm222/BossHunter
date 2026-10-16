import { NavLink } from 'react-router-dom'
import { RecruitIcon, type RecruitIconName } from '../recruiting/RecruitIcon'
import './recruiting-shell.css'
const main: { to: string; icon: RecruitIconName; label: string; end?: boolean }[] = [
  { to: '/recruiting', icon: 'workbench', label: '招聘工作台', end: true },
  { to: '/recruiting/positions', icon: 'jobs', label: '岗位与额度' },
  { to: '/recruiting/discover', icon: 'search', label: '主动打招呼' },
  { to: '/recruiting/candidates', icon: 'chat', label: '候选人沟通' },
  { to: '/recruiting/company', icon: 'company', label: '公司说明' },
]
const settings: typeof main = [
  { to: '/recruiting/requests', icon: 'meter', label: '请求统计' },
  { to: '/recruiting/monitor', icon: 'record', label: '运行记录' },
  { to: '/recruiting/settings', icon: 'settings', label: '模型设置' },
]
export function Sidebar() {
  return <aside className="recruit-sidebar">
    <NavLink to="/recruiting" className="recruit-brand" aria-label="BossHunter 招聘助手首页"><RecruitIcon name="brand" size={36} /><span><strong>BossHunter</strong><small>招聘助手</small></span></NavLink>
    <nav aria-label="招聘导航" className="recruit-navigation">
      <div className="recruit-nav-main">{main.map(item => <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `recruit-nav-link${isActive ? ' active' : ''}`}><RecruitIcon name={item.icon} /><span>{item.label}</span></NavLink>)}</div>
      <div className="recruit-nav-bottom">{settings.map(item => <NavLink key={item.to} to={item.to} className={({ isActive }) => `recruit-nav-link${isActive ? ' active' : ''}`}><RecruitIcon name={item.icon} size={22} /><span>{item.label}</span></NavLink>)}</div>
    </nav>
    <div className="recruit-sidebar-note"><span className="recruit-status-dot" />本地试运行<small>面试邀约发送已关闭</small></div>
  </aside>
}

import { Link, useLocation } from 'react-router-dom'
import { Bell, ChevronRight, Home } from 'lucide-react'
const pageTitles: Record<string, string> = {
  '/': '工作台', '/jobs': '岗位池', '/monitor': '监测执行', '/config': '配置',
  '/recruiting': '招聘工作台', '/recruiting/company': '公司说明',
  '/recruiting/discover': '主动打招呼', '/recruiting/positions': '岗位与每日额度',
  '/recruiting/candidates': '候选人沟通', '/recruiting/knowledge': '公司说明',
  '/recruiting/invitations': '候选人沟通', '/recruiting/monitor': '运行记录',
  '/recruiting/settings': '模型设置',
}
export function Header() {
  const location = useLocation()
  const title = pageTitles[location.pathname] || (location.pathname.startsWith('/recruiting/candidates/') ? '候选人详情' : 'BossHunter')
  return <header className="recruit-header"><div className="recruit-breadcrumb"><Link to="/recruiting" aria-label="返回招聘工作台"><Home size={15} /></Link><ChevronRight size={13} /><span>{title}</span></div><div className="recruit-header-tools"><span className="recruit-local-label"><span className="recruit-status-dot" />本地工作区</span><Link className="recruit-notification" to="/recruiting/monitor" aria-label="查看运行记录"><Bell size={18} /></Link><span className="recruit-local-avatar" aria-hidden="true">本</span></div></header>
}

import { useState, useEffect } from 'react';
import Sidebar from './components/Sidebar.jsx';
import ChatPage from './components/chat/ChatPage.jsx';
import ConfigPage from './components/config/ConfigPage.jsx';
import AdminPage from './components/admin/AdminPage.jsx';
import ConsolePage from './components/console/ConsolePage.jsx';
import { getMe } from './api.js';

function LoginOverlay({ error }) {
  return (
    <div className="login-overlay">
      <div className="login-card">
        <div className="login-logo">⬡</div>
        <h1>DevOps Agent</h1>
        <p className="login-subtitle">Sign in to manage your deployments</p>
        <a href="/auth/login" className="btn-ms-login">
          <svg width="20" height="20" viewBox="0 0 21 21" xmlns="http://www.w3.org/2000/svg">
            <rect x="1" y="1" width="9" height="9" fill="#f25022"/>
            <rect x="11" y="1" width="9" height="9" fill="#7fba00"/>
            <rect x="1" y="11" width="9" height="9" fill="#00a4ef"/>
            <rect x="11" y="11" width="9" height="9" fill="#ffb900"/>
          </svg>
          Sign in with Microsoft
        </a>
        {error && <p className="login-error">{error}</p>}
      </div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authError, setAuthError] = useState(null);
  const [page, setPage] = useState('chat');
  const [activeProject, setActiveProject] = useState(null);
  const [sidebarKey, setSidebarKey] = useState(0);
  const [autoSend, setAutoSend] = useState(null);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const err = params.get('auth_error');
    if (err) {
      history.replaceState({}, '', '/');
      setAuthError(`Sign-in failed: ${err.replace(/_/g, ' ')}`);
      setAuthChecked(true);
      return;
    }
    getMe().then(u => {
      setUser(u);
      setAuthChecked(true);
    });
  }, []);

  if (!authChecked) return null;
  if (!user) return <LoginOverlay error={authError} />;

  const isDevops = user.role === 'admin' || user.role === 'devops';

  return (
    <div className="layout">
      <Sidebar
        user={user}
        page={page}
        setPage={setPage}
        activeProject={activeProject}
        setActiveProject={setActiveProject}
        isDevops={isDevops}
        refreshKey={sidebarKey}
        onRequestClick={(r) => {
          setActiveProject(r.project_name);
          setPage('chat');
          setAutoSend(`Review deployment request #${r.id} for project ${r.project_name} (${r.environment})`);
        }}
      />
      <div className="main-area" style={{ minHeight: 0 }}>
        {page === 'chat' && (
          <ChatPage
            user={user}
            activeProject={activeProject}
            isDevops={isDevops}
            onRefreshSidebar={() => setSidebarKey(k => k + 1)}
            autoSend={autoSend}
          />
        )}
        {page === 'config' && isDevops && <ConfigPage />}
        {page === 'admin' && user.role === 'admin' && <AdminPage />}
        {page === 'console' && isDevops && <ConsolePage />}
      </div>
    </div>
  );
}

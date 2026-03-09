import { useState, useEffect } from 'react';
import { getProjects, getProjectEnvs, getDeployments, getDeploymentRequests, getDeploymentRequestsCount } from '../api.js';

function timeAgo(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  const diff = Math.floor((Date.now() - d) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

const STATUS_STYLE = {
  pending_review: { color: '#fcd34d', background: 'rgba(245,158,11,0.2)', border: '1px solid rgba(245,158,11,0.4)' },
  approved:       { color: '#6ee7b7', background: 'rgba(16,185,129,0.2)', border: '1px solid rgba(16,185,129,0.4)' },
  deployed:       { color: '#a5b4fc', background: 'rgba(99,102,241,0.2)', border: '1px solid rgba(99,102,241,0.4)' },
  rejected:       { color: '#fca5a5', background: 'rgba(239,68,68,0.2)',  border: '1px solid rgba(239,68,68,0.4)'  },
};

function ProjectItem({ project, isActive, onClick }) {
  const [expanded, setExpanded] = useState(false);
  const [envs, setEnvs] = useState(null);

  const status = project.latest_request_status;
  const statusStyle = STATUS_STYLE[status];

  function toggle(e) {
    e.stopPropagation();
    if (!expanded && envs === null) {
      getProjectEnvs(project.name).then(setEnvs).catch(() => setEnvs([]));
    }
    setExpanded(x => !x);
  }

  return (
    <li
      className={`sb-project-item${isActive ? ' active-project' : ''}${expanded ? ' expanded' : ''}`}
      onClick={() => onClick(project.name)}
    >
      <div className="sb-project-header" onClick={toggle}>
        <div className="sb-project-meta">
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span className="sb-project-name">{project.name}</span>
            {status && (
              <span style={{ fontSize: '0.65rem', fontWeight: 600, padding: '1px 6px', borderRadius: 10, whiteSpace: 'nowrap', ...statusStyle }}>
                {status.replace('_', ' ')}
              </span>
            )}
          </div>
          {project.github_repo && (
            <span className="sb-project-repo">{project.github_repo}</span>
          )}
        </div>
        <span className="sb-chevron">{expanded ? '▴' : '▾'}</span>
      </div>
      {expanded && (
        <div className="sb-project-detail">
          {envs === null ? (
            <div className="sb-envs-loading">Loading…</div>
          ) : envs.length === 0 ? (
            <div className="sb-envs-empty">No environments yet</div>
          ) : (
            envs.map(env => (
              <div key={env.name} className="sb-env-row">
                <div className="sb-env-top">
                  <span className="sb-env-icon">◈</span>
                  <span className="sb-env-name">{env.name}</span>
                </div>
                {env.last_image && (
                  <div className="sb-env-image">{env.last_image}</div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </li>
  );
}

export default function Sidebar({ user, page, setPage, activeProject, setActiveProject, isDevops, refreshKey, onRequestClick }) {
  const [projects, setProjects] = useState([]);
  const [requests, setRequests] = useState([]);
  const [requestCount, setRequestCount] = useState(0);

  useEffect(() => {
    loadData();
  }, [refreshKey]);

  async function loadData() {
    try {
      const projs = await getProjects();
      setProjects(projs || []);
    } catch (_) {}
    if (isDevops) {
      try {
        const reqs = await getDeploymentRequests(20);
        setRequests(reqs || []);
        const cnt = await getDeploymentRequestsCount();
        setRequestCount(cnt?.count || 0);
      } catch (_) {}
    }
  }

  const persona = isDevops ? 'devops' : 'developer';
  const displayName = user.display_name || user.username || 'User';

  return (
    <aside className="sidebar">
      {/* Brand */}
      <div className="sidebar-brand">
        <div className="sidebar-logo-icon" style={{ background: 'rgba(99,102,241,0.3)', fontSize: '0.9rem' }}>⬡</div>
        <div className="sidebar-logo-text">
          <div className="name">DevOps Agent</div>
          <div className="tagline">Deployment Manager</div>
        </div>
      </div>

      {/* Nav */}
      <div style={{ padding: '8px 12px 0', display: 'flex', flexDirection: 'column', gap: '2px' }}>
        <button
          className={`sb-nav-btn${page === 'chat' ? ' active' : ''}`}
          onClick={() => setPage('chat')}
        >
          <span style={{ fontSize: '0.8rem' }}>◫</span> Chat
        </button>
        {isDevops && (
          <button
            className={`sb-nav-btn${page === 'config' ? ' active' : ''}`}
            onClick={() => setPage('config')}
          >
            <span style={{ fontSize: '0.8rem' }}>◈</span> Environments
          </button>
        )}
        {user.role === 'admin' && (
          <button
            className={`sb-nav-btn${page === 'admin' ? ' active' : ''}`}
            onClick={() => setPage('admin')}
          >
            <span style={{ fontSize: '0.8rem' }}>◉</span> Settings
          </button>
        )}
      </div>

      {/* Projects */}
      {page === 'chat' && (
        <div className="sidebar-section">
          <div className="sidebar-section-title">Projects</div>
          <ul className="item-list">
            {projects.length === 0 ? (
              <li className="muted">No projects yet</li>
            ) : (
              projects.map(p => (
                <ProjectItem
                  key={p.name}
                  project={p}
                  isActive={activeProject === p.name}
                  onClick={setActiveProject}
                />
              ))
            )}
          </ul>
        </div>
      )}

      {/* Deployment Requests (devops/admin) — deduplicated: latest per project */}
      {isDevops && requests.length > 0 && (() => {
        const seen = new Set();
        const deduped = requests.filter(r => {
          const key = `${r.project_name}::${r.environment}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        });
        return (
        <div className="sidebar-section" style={{ flex: '0 0 auto', maxHeight: '220px' }}>
          <hr className="sb-divider" style={{ margin: '0 12px 8px' }} />
          <div className="sidebar-section-title">
            Deployment Requests
            {requestCount > 0 && (
              <span className="requests-badge">{requestCount}</span>
            )}
          </div>
          <ul className="item-list">
            {deduped.map(r => (
              <li key={r.id} className="request-item" style={{ cursor: 'pointer' }} onClick={() => onRequestClick?.(r)}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ fontWeight: 600, color: '#d4d4d4', fontSize: '0.8rem' }}>
                    {r.project_name}
                  </span>
                  <span className={`request-status ${r.status}`}>{r.status}</span>
                </div>
                <div className="req-meta">{r.environment} · {timeAgo(r.created_at)}</div>
              </li>
            ))}
          </ul>
        </div>
        );
      })()}

      {/* Footer */}
      <div className="sidebar-footer" style={{ marginTop: 'auto' }}>
        <div className="user-info">
          <div className="user-avatar">{(displayName[0] || '?').toUpperCase()}</div>
          <div className="user-details">
            <div className="user-name">{displayName}</div>
            <div className="user-role">
              {persona === 'devops' ? 'DevOps' : 'Developer'} · {user.role}
            </div>
          </div>
          <button
            className="logout-btn"
            title="Sign out"
            onClick={() => { window.location.href = '/auth/logout'; }}
          >
            →
          </button>
        </div>
      </div>
    </aside>
  );
}

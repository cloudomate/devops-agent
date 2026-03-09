import { useState, useEffect } from 'react';
import {
  getProjects, createProject, updateProject, deleteProject,
  getGlobalEnvs, createGlobalEnv, updateGlobalEnv, deleteGlobalEnv,
  getProjectEnvs, createProjectEnv, updateProjectEnv, deleteProjectEnv,
} from '../../api.js';

// ── Shared env form (global and project-specific) ────────────────────────────
function EnvForm({ initial, onSave, onCancel, projectName }) {
  // Agent reads from config.values.* (uppercase keys) for gitops/argocd/registry tokens.
  // Cloudflare config lives at config.cloudflare.*.
  const initVals = initial?.config?.values || {};
  const initCF   = initial?.config?.cloudflare || {};
  const isGlobal = !projectName;

  const [f, setF] = useState({
    name:          initial?.name || '',
    type:          initial?.type || 'kubernetes',
    // GitHub
    GITHUB_TOKEN:  initVals.GITHUB_TOKEN  || '',
    // GitOps
    GITOPS_REPO:   initVals.GITOPS_REPO   || '',
    GITOPS_BRANCH: initVals.GITOPS_BRANCH || 'main',
    GITOPS_TOKEN:  initVals.GITOPS_TOKEN  || '',
    // ArgoCD
    ARGOCD_URL:    initVals.ARGOCD_URL    || '',
    ARGOCD_TOKEN:  initVals.ARGOCD_TOKEN  || '',
    // Container registry
    REGISTRY:      initVals.REGISTRY      || '',
    // Common services
    POSTGRES_URL:  initVals.POSTGRES_URL  || '',
    REDIS_URL:     initVals.REDIS_URL     || '',
    MONGO_URL:     initVals.MONGO_URL     || '',
    // Cloudflare
    cf_enabled:    initCF.tunnel_enabled  || false,
    cf_api_token:  initCF.api_token       || '',
    cf_zone_id:    initCF.zone_id         || '',
    cf_account_id: initCF.account_id      || '',
    cf_tunnel_id:  initCF.tunnel_id       || '',
  });
  const [err, setErr] = useState('');

  const set  = k => e => setF(p => ({ ...p, [k]: e.target.value }));
  const setB = k => e => setF(p => ({ ...p, [k]: e.target.checked }));

  async function submit(e) {
    e.preventDefault();
    setErr('');

    const vKeys = [
      'GITOPS_REPO','GITOPS_BRANCH','GITOPS_TOKEN','GITHUB_TOKEN',
      'ARGOCD_URL','ARGOCD_TOKEN','REGISTRY',
      'POSTGRES_URL','REDIS_URL','MONGO_URL',
    ];
    const values = {};
    for (const k of vKeys) { if (f[k]) values[k] = f[k]; }

    const payload = {
      name: f.name,
      type: f.type,
      health_check_url: '',
      config: {
        values,
        ...(f.cf_enabled || f.cf_api_token ? {
          cloudflare: {
            tunnel_enabled: f.cf_enabled,
            api_token:  f.cf_api_token  || '',
            zone_id:    f.cf_zone_id    || '',
            account_id: f.cf_account_id || '',
            tunnel_id:  f.cf_tunnel_id  || '',
          },
        } : {}),
      },
    };

    try {
      if (isGlobal) {
        if (initial?.name) await updateGlobalEnv(initial.name, payload);
        else               await createGlobalEnv(payload);
      } else {
        if (initial?.name) await updateProjectEnv(projectName, initial.name, payload);
        else               await createProjectEnv(projectName, payload);
      }
      onSave();
    } catch (ex) { setErr(ex.message); }
  }

  const hint = isGlobal
    ? 'Fallback for all projects'
    : `Override for project "${projectName}" — overrides global defaults`;

  return (
    <form className="config-form" onSubmit={submit}>
      {!isGlobal && (
        <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0 0 8px' }}>
          {hint}. Leave fields blank to inherit from the global environment.
        </p>
      )}
      <div className="form-row">
        <label>Name*</label>
        <input value={f.name} onChange={set('name')} placeholder="staging" required disabled={!!initial?.name} />
      </div>
      <div className="form-row">
        <label>Type*</label>
        <select value={f.type} onChange={set('type')}>
          <option value="kubernetes">Kubernetes</option>
          <option value="ssh">SSH</option>
          <option value="docker_compose">Docker Compose</option>
        </select>
      </div>

      <div className="gef-section-title">GitHub Access</div>
      <div className="form-row">
        <label>GitHub Token</label>
        <input type="password" value={f.GITHUB_TOKEN} onChange={set('GITHUB_TOKEN')}
          placeholder="ghp_… (repo read for discovery)" />
      </div>

      <div className="gef-section-title">GitOps Repository</div>
      <div className="form-row">
        <label>GitOps Repo</label>
        <input value={f.GITOPS_REPO} onChange={set('GITOPS_REPO')} placeholder="myorg/gitops" />
      </div>
      <div className="form-row">
        <label>Branch</label>
        <input value={f.GITOPS_BRANCH} onChange={set('GITOPS_BRANCH')} placeholder="main" />
      </div>
      <div className="form-row">
        <label>GitOps Token</label>
        <input type="password" value={f.GITOPS_TOKEN} onChange={set('GITOPS_TOKEN')}
          placeholder="ghp_… (write access to gitops repo)" />
      </div>

      <div className="gef-section-title">Container Registry</div>
      <div className="form-row">
        <label>Registry prefix</label>
        <input value={f.REGISTRY} onChange={set('REGISTRY')} placeholder="cr.imys.in/hci or ghcr.io/myorg" />
      </div>

      <div className="gef-section-title">ArgoCD</div>
      <div className="form-row">
        <label>ArgoCD URL</label>
        <input value={f.ARGOCD_URL} onChange={set('ARGOCD_URL')} placeholder="http://argocd.imys.in" />
      </div>
      <div className="form-row">
        <label>ArgoCD Token</label>
        <input type="password" value={f.ARGOCD_TOKEN} onChange={set('ARGOCD_TOKEN')} placeholder="API token" />
      </div>

      <div className="gef-section-title">Common Services</div>
      <div className="form-row">
        <label>Postgres URL</label>
        <input type="password" value={f.POSTGRES_URL} onChange={set('POSTGRES_URL')}
          placeholder="postgresql://user:pass@host:5432/db" />
      </div>
      <div className="form-row">
        <label>Redis URL</label>
        <input type="password" value={f.REDIS_URL} onChange={set('REDIS_URL')}
          placeholder="redis://host:6379/0" />
      </div>
      <div className="form-row">
        <label>MongoDB URL</label>
        <input type="password" value={f.MONGO_URL} onChange={set('MONGO_URL')}
          placeholder="mongodb://user:pass@host:27017/db" />
      </div>

      <div className="gef-section-title">Cloudflare Tunnel</div>
      <div className="form-row">
        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input type="checkbox" checked={f.cf_enabled} onChange={setB('cf_enabled')} style={{ width: 'auto', margin: 0 }} />
          Use Tunnel
          <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)', fontWeight: 'normal' }}>
            — ClusterIP only, no nginx Ingress
          </span>
        </label>
      </div>
      <div className="form-row">
        <label>CF API Token</label>
        <input type="password" value={f.cf_api_token} onChange={set('cf_api_token')}
          placeholder="Cloudflare API token" />
      </div>
      <div className="form-row">
        <label>Zone ID</label>
        <input value={f.cf_zone_id} onChange={set('cf_zone_id')} placeholder="Zone ID (for DNS management)" />
      </div>
      <div className="form-row">
        <label>Account ID</label>
        <input value={f.cf_account_id} onChange={set('cf_account_id')} placeholder="Account ID (for tunnel API)" />
      </div>
      <div className="form-row">
        <label>Tunnel ID</label>
        <input value={f.cf_tunnel_id} onChange={set('cf_tunnel_id')} placeholder="Tunnel UUID" />
      </div>

      <div className="form-actions">
        <button type="submit" className="btn-sm btn-primary">Save</button>
        <button type="button" className="btn-sm btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
      {err && <p className="form-error">{err}</p>}
    </form>
  );
}

// ── Env summary subtitle ──────────────────────────────────────────────────────
function EnvSubtitle({ env }) {
  const v = env.config?.values || {};
  const cf = env.config?.cloudflare || {};
  return (
    <div className="config-item-sub">
      {env.type}
      {v.GITOPS_REPO  ? ` · ${v.GITOPS_REPO}` : ' · no gitops repo'}
      {v.ARGOCD_URL   ? ` · ${v.ARGOCD_URL}`  : ''}
      {v.REGISTRY     ? ` · ${v.REGISTRY}`     : ''}
      {v.POSTGRES_URL ? ' · postgres' : ''}
      {v.REDIS_URL    ? ' · redis'    : ''}
      {v.MONGO_URL    ? ' · mongo'    : ''}
      {cf.tunnel_enabled ? ' · CF tunnel' : ''}
    </div>
  );
}

// ── Project Form ─────────────────────────────────────────────────────────────
function ProjectForm({ initial, onSave, onCancel }) {
  const [f, setF] = useState({ name: '', github_repo: '', description: '', ...initial });
  const [err, setErr] = useState('');
  const set = k => e => setF(p => ({ ...p, [k]: e.target.value }));

  async function submit(e) {
    e.preventDefault();
    setErr('');
    try {
      if (initial?.name) await updateProject(initial.name, f);
      else await createProject(f);
      onSave();
    } catch (ex) { setErr(ex.message); }
  }

  return (
    <form className="config-form" onSubmit={submit}>
      <div className="form-row">
        <label>Name*</label>
        <input value={f.name} onChange={set('name')} placeholder="my-api" required disabled={!!initial?.name} />
      </div>
      <div className="form-row">
        <label>GitHub Repo</label>
        <input value={f.github_repo || ''} onChange={set('github_repo')} placeholder="myorg/my-api" />
      </div>
      <div className="form-row">
        <label>Description</label>
        <input value={f.description || ''} onChange={set('description')} placeholder="Optional description" />
      </div>
      <div className="form-actions">
        <button type="submit" className="btn-sm btn-primary">Save</button>
        <button type="button" className="btn-sm btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
      {err && <p className="form-error">{err}</p>}
    </form>
  );
}

// ── Project row with expandable env management ────────────────────────────────
function ProjectRow({ project, onEdit, onDelete }) {
  const [expanded, setExpanded] = useState(false);
  const [envs, setEnvs] = useState(null);
  const [showEnvForm, setShowEnvForm] = useState(false);
  const [editEnv, setEditEnv] = useState(null);

  async function loadEnvs() {
    const data = await getProjectEnvs(project.name).catch(() => []);
    setEnvs(data || []);
  }

  function toggle() {
    if (!expanded && envs === null) loadEnvs();
    setExpanded(x => !x);
  }

  async function delEnv(envName) {
    if (!confirm(`Delete environment "${envName}" from "${project.name}"?`)) return;
    await deleteProjectEnv(project.name, envName).catch(e => alert(e.message));
    loadEnvs();
  }

  return (
    <li>
      <div className="config-item">
        <div className="config-item-info" style={{ cursor: 'pointer' }} onClick={toggle}>
          <div className="config-item-name">
            {project.name}
            <span style={{ marginLeft: 8, fontSize: '0.7rem', color: 'var(--text-muted)' }}>
              {expanded ? '▴' : '▾'}
            </span>
          </div>
          <div className="config-item-sub">{project.github_repo || 'no repo'} {project.description ? `· ${project.description}` : ''}</div>
        </div>
        <div className="config-item-actions">
          <button className="btn-sm btn-ghost" onClick={() => onEdit(project)}>Edit</button>
          <button className="btn-sm btn-danger" onClick={() => onDelete(project.name)}>Delete</button>
        </div>
      </div>

      {expanded && (
        <div style={{ marginLeft: 16, marginBottom: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0 4px' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Environments
            </span>
            <button className="btn-sm btn-primary" style={{ fontSize: '0.7rem', padding: '2px 8px' }}
              onClick={() => { setEditEnv(null); setShowEnvForm(true); }}>
              + Add
            </button>
          </div>

          {showEnvForm && (
            <EnvForm
              initial={editEnv}
              projectName={project.name}
              onSave={() => { setShowEnvForm(false); setEditEnv(null); loadEnvs(); }}
              onCancel={() => { setShowEnvForm(false); setEditEnv(null); }}
            />
          )}

          {envs === null ? (
            <div className="muted" style={{ fontSize: '0.8rem', padding: '4px 0' }}>Loading…</div>
          ) : envs.length === 0 ? (
            <div className="muted" style={{ fontSize: '0.8rem', padding: '4px 0' }}>No environments — inherits global defaults</div>
          ) : (
            <ul className="config-list" style={{ margin: 0 }}>
              {envs.map(env => (
                <li key={env.name}>
                  <div className="config-item">
                    <div className="config-item-info">
                      <div className="config-item-name" style={{ fontSize: '0.85rem' }}>{env.name}</div>
                      <EnvSubtitle env={env} />
                    </div>
                    <div className="config-item-actions">
                      <button className="btn-sm btn-ghost" onClick={() => { setEditEnv(env); setShowEnvForm(true); }}>Edit</button>
                      <button className="btn-sm btn-danger" onClick={() => delEnv(env.name)}>Delete</button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

// ── Main Config Page ─────────────────────────────────────────────────────────
export default function ConfigPage() {
  const [globalEnvs, setGlobalEnvs] = useState([]);
  const [projects, setProjects] = useState([]);
  const [showGEForm, setShowGEForm] = useState(false);
  const [editGE, setEditGE] = useState(null);
  const [showPForm, setShowPForm] = useState(false);
  const [editP, setEditP] = useState(null);

  useEffect(() => { load(); }, []);

  async function load() {
    const [ge, pr] = await Promise.all([
      getGlobalEnvs().catch(() => []),
      getProjects().catch(() => []),
    ]);
    setGlobalEnvs(ge || []);
    setProjects(pr || []);
  }

  async function delGE(name) {
    if (!confirm(`Delete environment "${name}"?`)) return;
    await deleteGlobalEnv(name).catch(e => alert(e.message));
    load();
  }

  async function delP(name) {
    if (!confirm(`Delete project "${name}"?`)) return;
    await deleteProject(name).catch(e => alert(e.message));
    load();
  }

  return (
    <div className="page page-active" style={{ display: 'flex', flexDirection: 'column', flex: 1 }}>
      <header className="top-header">
        <h1>Environments &amp; Projects</h1>
        <span className="header-hint">Configure global environments and projects</span>
      </header>
      <div className="page-body">

        {/* Global Environments */}
        <div className="config-section">
          <div className="config-section-header">
            <h3>Global Environments <span className="global-env-hint">fallback for all projects</span></h3>
            <button className="btn-sm btn-primary" onClick={() => { setShowGEForm(true); setEditGE(null); }}>
              + Add Environment
            </button>
          </div>
          {showGEForm && (
            <EnvForm
              initial={editGE}
              onSave={() => { setShowGEForm(false); setEditGE(null); load(); }}
              onCancel={() => { setShowGEForm(false); setEditGE(null); }}
            />
          )}
          <ul className="config-list">
            {globalEnvs.length === 0 ? (
              <li className="muted">No environments yet</li>
            ) : globalEnvs.map(env => (
              <li key={env.name}>
                <div className="config-item">
                  <div className="config-item-info">
                    <div className="config-item-name">{env.name}</div>
                    <EnvSubtitle env={env} />
                  </div>
                  <div className="config-item-actions">
                    <button className="btn-sm btn-ghost" onClick={() => { setEditGE(env); setShowGEForm(true); }}>Edit</button>
                    <button className="btn-sm btn-danger" onClick={() => delGE(env.name)}>Delete</button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>

        {/* Projects */}
        <div className="config-section" style={{ marginTop: 32 }}>
          <div className="config-section-header">
            <h3>Projects <span className="global-env-hint">expand to manage per-project environments</span></h3>
            <button className="btn-sm btn-primary" onClick={() => { setShowPForm(true); setEditP(null); }}>
              + Add Project
            </button>
          </div>
          {showPForm && (
            <ProjectForm
              initial={editP}
              onSave={() => { setShowPForm(false); setEditP(null); load(); }}
              onCancel={() => { setShowPForm(false); setEditP(null); }}
            />
          )}
          <ul className="config-list">
            {projects.length === 0 ? (
              <li className="muted">No projects yet</li>
            ) : projects.map(p => (
              <ProjectRow
                key={p.name}
                project={p}
                onEdit={proj => { setEditP(proj); setShowPForm(true); }}
                onDelete={delP}
              />
            ))}
          </ul>
        </div>

      </div>
    </div>
  );
}

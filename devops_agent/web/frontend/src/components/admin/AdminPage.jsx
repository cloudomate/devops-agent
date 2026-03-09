import { useState, useEffect } from 'react';
import { getAdminSettings, saveAdminSettings, getUsers, updateUserRole, deleteUser } from '../../api.js';

function Section({ title, hint, children }) {
  return (
    <section className="admin-section">
      <h2 className="admin-section-title">{title}</h2>
      {hint && <p className="admin-section-hint">{hint}</p>}
      <div className="admin-card">{children}</div>
    </section>
  );
}

function SaveRow({ id, onSave, status }) {
  return (
    <div className="form-actions">
      <button className="btn-sm btn-primary" onClick={onSave}>Save</button>
      {status && <span className={`admin-save-status ${status.ok ? 'ok' : 'err'}`}>{status.msg}</span>}
    </div>
  );
}

function Field({ label, id, type = 'text', value, onChange, placeholder }) {
  return (
    <div className="form-row" style={{ gridTemplateColumns: '160px 1fr' }}>
      <label htmlFor={id}>{label}</label>
      <input id={id} type={type} value={value} onChange={onChange} placeholder={placeholder}
        autoComplete={type === 'password' ? 'new-password' : undefined} />
    </div>
  );
}

export default function AdminPage() {
  const [settings, setSettings] = useState({});
  const [users, setUsers] = useState([]);
  const [userSearch, setUserSearch] = useState('');
  const [statuses, setStatuses] = useState({});

  useEffect(() => {
    getAdminSettings().then(s => {
      // Normalize all keys to lowercase so form bindings work
      const normalized = {};
      for (const [k, v] of Object.entries(s || {})) normalized[k.toLowerCase()] = v;
      setSettings(normalized);
    }).catch(() => {});
    getUsers().then(u => setUsers(u || [])).catch(() => {});
  }, []);

  function set(k) { return e => setSettings(p => ({ ...p, [k]: e.target.value })); }

  async function save(section) {
    try {
      await saveAdminSettings(settings);
      setStatuses(p => ({ ...p, [section]: { ok: true, msg: 'Saved' } }));
      setTimeout(() => setStatuses(p => ({ ...p, [section]: null })), 3000);
    } catch (e) {
      setStatuses(p => ({ ...p, [section]: { ok: false, msg: e.message } }));
    }
  }

  async function changeRole(uid, role) {
    await updateUserRole(uid, role).catch(e => alert(e.message));
    getUsers().then(u => setUsers(u || [])).catch(() => {});
  }

  async function delUser(uid) {
    if (!confirm('Remove this user?')) return;
    await deleteUser(uid).catch(e => alert(e.message));
    getUsers().then(u => setUsers(u || [])).catch(() => {});
  }

  const filteredUsers = users.filter(u =>
    !userSearch || u.display_name?.toLowerCase().includes(userSearch.toLowerCase())
      || u.username?.toLowerCase().includes(userSearch.toLowerCase())
  );

  return (
    <div className="page page-active" style={{ display: 'flex', flexDirection: 'column', flex: 1 }}>
      <header className="top-header">
        <h1>Admin Settings</h1>
        <span className="header-hint">System-level configuration</span>
      </header>
      <div className="admin-page-body">

        <Section title="LLM / AI" hint="Language model settings. Changes apply immediately.">
          <Field label="Model ID" id="llm-model" value={settings.llm_model || ''} onChange={set('llm_model')} placeholder="Qwen/Qwen3-Coder-Next (case-sensitive)" />
          <Field label="Base URL" id="llm-url" value={settings.llm_base_url || ''} onChange={set('llm_base_url')} placeholder="https://api.anthropic.com/v1" />
          <Field label="API Key" id="llm-key" type="password" value={settings.llm_api_key || ''} onChange={set('llm_api_key')} placeholder="sk-ant-…" />
          <SaveRow onSave={() => save('llm')} status={statuses.llm} />
        </Section>

        <Section title="SSO / OIDC (Microsoft Entra)" hint="Leave blank to run in no-auth dev mode.">
          <Field label="Tenant ID" id="entra-tenant" value={settings.entra_tenant_id || ''} onChange={set('entra_tenant_id')} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
          <Field label="Client ID" id="entra-client" value={settings.entra_client_id || ''} onChange={set('entra_client_id')} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
          <Field label="Client Secret" id="entra-secret" type="password" value={settings.entra_client_secret || ''} onChange={set('entra_client_secret')} placeholder="(stored as secret)" />
          <Field label="Redirect URI" id="entra-redirect" value={settings.entra_redirect_uri || ''} onChange={set('entra_redirect_uri')} placeholder="https://your-domain/auth/callback" />
          <Field label="Admin Group ID" id="entra-admin-group" value={settings.entra_admin_group_id || ''} onChange={set('entra_admin_group_id')} placeholder="Entra group object ID → admin role" />
          <SaveRow onSave={() => save('oidc')} status={statuses.oidc} />
        </Section>

        <Section title="GitHub &amp; GitOps" hint="Global GitHub token and GitOps defaults.">
          <Field label="GitHub Token" id="gh-token" type="password" value={settings.github_token || ''} onChange={set('github_token')} placeholder="ghp_…" />
          <Field label="GitOps Repo" id="gitops-repo" value={settings.gitops_repo || ''} onChange={set('gitops_repo')} placeholder="myorg/gitops" />
          <Field label="GitOps Branch" id="gitops-branch" value={settings.gitops_branch || ''} onChange={set('gitops_branch')} placeholder="main" />
          <Field label="ArgoCD URL" id="argocd-url" value={settings.argocd_url || ''} onChange={set('argocd_url')} placeholder="http://argocd.imys.in" />
          <Field label="ArgoCD Token" id="argocd-token" type="password" value={settings.argocd_token || ''} onChange={set('argocd_token')} placeholder="eyJ…" />
          <SaveRow onSave={() => save('github')} status={statuses.github} />
        </Section>

        <Section title="Users &amp; Roles" hint="Role hierarchy: Admin > DevOps > Developer.">
          <div className="admin-users-toolbar">
            <input
              type="text"
              className="admin-search"
              placeholder="Search users…"
              value={userSearch}
              onChange={e => setUserSearch(e.target.value)}
            />
          </div>
          <table className="admin-table">
            <thead>
              <tr>
                <th>User</th>
                <th>Username</th>
                <th>Role</th>
                <th>Joined</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filteredUsers.length === 0 ? (
                <tr><td colSpan={5} className="muted" style={{ textAlign: 'center', padding: 16 }}>No users</td></tr>
              ) : filteredUsers.map(u => (
                <tr key={u.id}>
                  <td>{u.display_name || u.username}</td>
                  <td>{u.username}</td>
                  <td>
                    <select value={u.role} onChange={e => changeRole(u.id, e.target.value)}>
                      <option value="developer">Developer</option>
                      <option value="devops">DevOps</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                  <td style={{ color: 'var(--text-muted)', fontSize: '0.78rem' }}>
                    {u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}
                  </td>
                  <td>
                    <button className="btn-sm btn-danger" onClick={() => delUser(u.id)}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>

      </div>
    </div>
  );
}

// ── API fetch wrappers ─────────────────────────────────────────────────────────

async function apiFetch(url, options = {}) {
  const r = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  if (r.status === 204) return null;
  return r.json();
}

// Auth
export const getMe = () => fetch('/auth/me').then(r => r.ok ? r.json() : null).catch(() => null);

// Projects
export const getProjects = () => apiFetch('/api/projects');
export const createProject = (body) => apiFetch('/api/projects', { method: 'POST', body: JSON.stringify(body) });
export const updateProject = (name, body) => apiFetch(`/api/projects/${encodeURIComponent(name)}`, { method: 'PATCH', body: JSON.stringify(body) });
export const deleteProject = (name) => apiFetch(`/api/projects/${encodeURIComponent(name)}`, { method: 'DELETE' });

// Environments (per-project)
export const getProjectEnvs = (project) => apiFetch(`/api/projects/${encodeURIComponent(project)}/environments`);
export const createProjectEnv = (project, body) => apiFetch(`/api/projects/${encodeURIComponent(project)}/environments`, { method: 'POST', body: JSON.stringify(body) });
export const updateProjectEnv = (project, name, body) => apiFetch(`/api/projects/${encodeURIComponent(project)}/environments/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteProjectEnv = (project, name) => apiFetch(`/api/projects/${encodeURIComponent(project)}/environments/${encodeURIComponent(name)}`, { method: 'DELETE' });
export const getRepoValues = (project) => apiFetch(`/api/projects/${encodeURIComponent(project)}/repo-values`);

// Global Environments
export const getGlobalEnvs = () => apiFetch('/api/environments/global');
export const createGlobalEnv = (body) => apiFetch('/api/environments/global', { method: 'POST', body: JSON.stringify(body) });
export const updateGlobalEnv = (name, body) => apiFetch(`/api/environments/global/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteGlobalEnv = (name) => apiFetch(`/api/environments/global/${encodeURIComponent(name)}`, { method: 'DELETE' });

// Deployments
export const getDeployments = (limit = 10) => apiFetch(`/api/deployments?limit=${limit}`);

// Deployment Requests
export const getDeploymentRequests = (limit = 20) => apiFetch(`/api/deployment-requests?limit=${limit}`);
export const getDeploymentRequestsCount = () => apiFetch('/api/deployment-requests/count');

// Chat history
export const getChatHistory = (sessionId) => apiFetch(`/api/chat-history/${sessionId}`);

// Admin settings
export const getAdminSettings = () => apiFetch('/api/admin/settings');
export const saveAdminSettings = (settings) => apiFetch('/api/admin/settings', { method: 'PUT', body: JSON.stringify({ settings }) });

// Users
export const getUsers = () => apiFetch('/api/users');
export const updateUserRole = (uid, role) => apiFetch(`/api/users/${uid}/role`, { method: 'PATCH', body: JSON.stringify({ role }) });
export const deleteUser = (uid) => apiFetch(`/api/users/${uid}`, { method: 'DELETE' });

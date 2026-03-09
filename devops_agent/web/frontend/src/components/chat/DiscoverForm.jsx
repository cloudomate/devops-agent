import { useState } from 'react';

const ENV_OPTIONS = ['staging', 'prod', 'dev'];

export default function DiscoverForm({ data, onSubmit }) {
  const [submitted, setSubmitted] = useState(false);

  // Environments
  const [envs, setEnvs] = useState(() => {
    if (data.suggested_environments?.length) return data.suggested_environments;
    return ['staging', 'prod'];
  });
  const [customEnv, setCustomEnv] = useState('');

  // Domains: { staging: '', prod: '' }
  const [domains, setDomains] = useState(() => {
    const d = {};
    (data.suggested_environments || ['staging', 'prod']).forEach(e => { d[e] = ''; });
    return d;
  });

  // Registry
  const [registry, setRegistry] = useState(data.suggested_registry || '');

  // Env vars: [{ key, value, is_secret }]
  const [envVars, setEnvVars] = useState(() =>
    (data.required_env_vars || []).map(k => ({ key: k, value: '', is_secret: true }))
  );
  const [newVarKey, setNewVarKey] = useState('');

  // Notes
  const [notes, setNotes] = useState('');

  function toggleEnv(env) {
    setEnvs(prev => {
      const next = prev.includes(env) ? prev.filter(e => e !== env) : [...prev, env];
      // sync domains keys
      setDomains(d => {
        const nd = { ...d };
        if (!next.includes(env)) delete nd[env];
        else if (!(env in nd)) nd[env] = '';
        return nd;
      });
      return next;
    });
  }

  function addCustomEnv() {
    const e = customEnv.trim().toLowerCase().replace(/\s+/g, '-');
    if (!e || envs.includes(e)) { setCustomEnv(''); return; }
    setEnvs(prev => [...prev, e]);
    setDomains(d => ({ ...d, [e]: '' }));
    setCustomEnv('');
  }

  function setDomain(env, val) {
    setDomains(d => ({ ...d, [env]: val }));
  }

  function addEnvVar() {
    const k = newVarKey.trim().toUpperCase().replace(/\s+/g, '_');
    if (!k || envVars.some(v => v.key === k)) { setNewVarKey(''); return; }
    setEnvVars(prev => [...prev, { key: k, value: '', is_secret: true }]);
    setNewVarKey('');
  }

  function updateVar(i, field, val) {
    setEnvVars(prev => prev.map((v, idx) => idx === i ? { ...v, [field]: val } : v));
  }

  function removeVar(i) {
    setEnvVars(prev => prev.filter((_, idx) => idx !== i));
  }

  function handleSubmit() {
    // Build compact YAML for the agent
    const y = [];
    y.push(`[ONBOARDING FORM RESPONSE]`);
    y.push('```yaml');
    y.push(`environments: [${envs.join(', ')}]`);
    if (envs.some(e => domains[e])) {
      y.push('domains:');
      envs.forEach(e => y.push(`  ${e}: "${domains[e] || ''}"`));
    }
    if (registry.trim()) y.push(`registry: "${registry.trim()}"`);
    const vars = envVars.filter(v => v.key);
    if (vars.length) {
      y.push('env_vars:');
      vars.forEach(v => {
        const val = v.value ? (v.is_secret ? '[secret]' : v.value) : 'k8s_secret';
        y.push(`  ${v.key}: ${val}`);
      });
    }
    if (notes.trim()) y.push(`notes: "${notes.trim()}"`);
    y.push('```');

    const secretCount = vars.filter(v => !v.value || v.is_secret).length;
    const plainCount = vars.filter(v => v.value && !v.is_secret).length;
    const varSummary = vars.length
      ? `${vars.length} env vars${secretCount ? ` (${secretCount} secrets)` : ''}`
      : 'no extra env vars';
    const domainSummary = envs.map(e => domains[e] || e).join(', ');
    const display = `Onboarding form submitted — ${envs.join('+')}${registry.trim() ? ` · ${registry.trim()}` : ''} · ${varSummary}${notes.trim() ? ` · "${notes.trim()}"` : ''}`;

    setSubmitted(true);
    onSubmit?.(y.join('\n'), display);
  }

  const s = {
    card: { margin: '12px 0 4px', border: '1.5px solid var(--accent)', borderRadius: 8, background: 'var(--surface)', maxWidth: 520, fontSize: '0.82rem' },
    header: { display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderBottom: '1px solid var(--border)' },
    title: { fontSize: '.8rem', fontWeight: 700, color: 'var(--accent)', letterSpacing: '.04em', textTransform: 'uppercase' },
    body: { padding: '12px 14px 8px', display: 'flex', flexDirection: 'column', gap: 14 },
    section: { display: 'flex', flexDirection: 'column', gap: 6 },
    label: { fontSize: '.75rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: 2 },
    input: { padding: '5px 8px', borderRadius: 5, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.82rem', width: '100%', outline: 'none' },
    row: { display: 'flex', alignItems: 'center', gap: 6 },
    chip: (active) => ({
      padding: '3px 10px', borderRadius: 20, border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
      background: active ? 'color-mix(in srgb, var(--accent) 10%, transparent)' : 'transparent',
      color: active ? 'var(--accent)' : 'var(--text-muted)', cursor: 'pointer', fontSize: '0.78rem', fontWeight: 600,
    }),
    varRow: { display: 'grid', gridTemplateColumns: '1fr 1fr auto auto', gap: 4, alignItems: 'center' },
    secretToggle: (on) => ({
      padding: '3px 7px', borderRadius: 4, border: `1px solid ${on ? '#22c55e' : 'var(--border)'}`,
      background: on ? '#f0fdf4' : 'transparent', color: on ? '#15803d' : 'var(--text-muted)',
      cursor: 'pointer', fontSize: '0.7rem', whiteSpace: 'nowrap',
    }),
    removeBtn: { background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '1rem', padding: '0 3px', lineHeight: 1 },
    footer: { display: 'flex', gap: 8, padding: '10px 14px', borderTop: '1px solid var(--border)' },
    submitBtn: { background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: 6, padding: '6px 16px', fontSize: '0.82rem', fontWeight: 600, cursor: 'pointer' },
    detected: { background: 'var(--bg)', borderRadius: 5, padding: '6px 10px', fontSize: '0.78rem', color: 'var(--text-sub)', display: 'flex', flexWrap: 'wrap', gap: '6px 14px' },
    detectedItem: { display: 'flex', gap: 5 },
    detectedKey: { color: 'var(--text-muted)', fontWeight: 600 },
  };

  if (submitted) {
    return (
      <div style={s.card}>
        <div style={{ ...s.header, justifyContent: 'space-between' }}>
          <span style={s.title}>Onboarding Form</span>
          <button
            onClick={() => setSubmitted(false)}
            style={{ background: 'none', border: '1px solid var(--border)', borderRadius: 4, padding: '2px 10px', fontSize: '0.75rem', color: 'var(--text-muted)', cursor: 'pointer' }}
          >
            Edit
          </button>
        </div>
        <div style={{ padding: '12px 14px', color: 'var(--text-muted)', fontSize: '0.82rem', fontStyle: 'italic' }}>
          Submitted — processing…
        </div>
      </div>
    );
  }

  return (
    <div style={s.card}>
      <div style={s.header}>
        <span style={s.title}>Onboarding — fill in what's needed</span>
      </div>

      <div style={s.body}>
        {/* Detected info */}
        {(data.detected_stack || data.detected_port || data.has_dockerfile != null) && (
          <div style={s.detected}>
            {data.detected_stack && <span style={s.detectedItem}><span style={s.detectedKey}>Stack</span>{data.detected_stack}</span>}
            {data.detected_port && <span style={s.detectedItem}><span style={s.detectedKey}>Port</span>{data.detected_port}</span>}
            {data.has_dockerfile != null && <span style={s.detectedItem}><span style={s.detectedKey}>Dockerfile</span>{data.has_dockerfile ? 'yes' : 'no'}</span>}
            {data.health_path && <span style={s.detectedItem}><span style={s.detectedKey}>Health</span>{data.health_path}</span>}
          </div>
        )}

        {/* Environments */}
        <div style={s.section}>
          <div style={s.label}>Environments</div>
          <div style={s.row}>
            {ENV_OPTIONS.map(e => (
              <button key={e} style={s.chip(envs.includes(e))} onClick={() => toggleEnv(e)}>{e}</button>
            ))}
            {envs.filter(e => !ENV_OPTIONS.includes(e)).map(e => (
              <button key={e} style={s.chip(true)} onClick={() => toggleEnv(e)}>{e} ×</button>
            ))}
            <input
              style={{ ...s.input, width: 90 }}
              placeholder="custom…"
              value={customEnv}
              onChange={e => setCustomEnv(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addCustomEnv()}
            />
          </div>
        </div>

        {/* Domains */}
        {envs.length > 0 && (
          <div style={s.section}>
            <div style={s.label}>Domains</div>
            {envs.map(e => (
              <div key={e} style={s.row}>
                <span style={{ minWidth: 64, color: 'var(--text-muted)', fontWeight: 600, fontSize: '0.78rem' }}>{e}</span>
                <input
                  style={s.input}
                  placeholder={`app${e === 'prod' ? '' : `.${e}`}.example.com`}
                  value={domains[e] || ''}
                  onChange={ev => setDomain(e, ev.target.value)}
                />
              </div>
            ))}
          </div>
        )}

        {/* Registry */}
        <div style={s.section}>
          <div style={s.label}>Container Registry</div>
          <input style={s.input} placeholder="cr.example.com/org  (leave blank to discuss with DevOps)" value={registry} onChange={e => setRegistry(e.target.value)} />
        </div>

        {/* Env vars */}
        <div style={s.section}>
          <div style={s.label}>Environment Variables / Secrets</div>
          {envVars.map((v, i) => (
            <div key={i} style={s.varRow}>
              <input style={s.input} value={v.key} onChange={e => updateVar(i, 'key', e.target.value.toUpperCase())} placeholder="VAR_NAME" />
              <input style={s.input} value={v.value} onChange={e => updateVar(i, 'value', e.target.value)} placeholder={v.is_secret ? 'leave blank → k8s secret' : 'value'} />
              <button style={s.secretToggle(v.is_secret)} onClick={() => updateVar(i, 'is_secret', !v.is_secret)}>{v.is_secret ? 'secret' : 'plain'}</button>
              <button style={s.removeBtn} onClick={() => removeVar(i)}>×</button>
            </div>
          ))}
          <div style={s.row}>
            <input
              style={{ ...s.input, width: 180 }}
              placeholder="+ add variable…"
              value={newVarKey}
              onChange={e => setNewVarKey(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addEnvVar()}
            />
            <button style={{ ...s.chip(false), padding: '4px 10px' }} onClick={addEnvVar}>Add</button>
          </div>
        </div>

        {/* Notes */}
        <div style={s.section}>
          <div style={s.label}>Notes for DevOps (optional)</div>
          <textarea
            style={{ ...s.input, resize: 'vertical', minHeight: 52 }}
            placeholder="Anything special: DB migration on deploy, specific resource needs, etc."
            value={notes}
            onChange={e => setNotes(e.target.value)}
          />
        </div>
      </div>

      <div style={s.footer}>
        <button style={s.submitBtn} onClick={handleSubmit}>Submit to DevOps →</button>
      </div>
    </div>
  );
}

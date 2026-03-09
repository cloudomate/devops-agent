import { useState } from 'react';

export default function ConfirmCard({ data, onSubmit, onEdit }) {
  const [submitted, setSubmitted] = useState(false);
  const [editing, setEditing] = useState(false);

  function handleSubmit() {
    setSubmitted(true);
    onSubmit?.();
  }

  return (
    <div className="deploy-confirm-card">
      <div className="dcc-header">
        <span className="dcc-icon" style={{ fontSize: '0.9rem' }}>▶</span>
        <span className="dcc-title">Deployment Request Summary</span>
      </div>
      <div className="dcc-body">
        {data.project && (
          <div className="dcc-row">
            <span className="dcc-label">Project</span>
            <span className="dcc-val">{data.project}</span>
          </div>
        )}
        {data.environment && (
          <div className="dcc-row">
            <span className="dcc-label">Environment</span>
            <span className="dcc-val">{data.environment}</span>
          </div>
        )}
        {data.image && (
          <div className="dcc-row">
            <span className="dcc-label">Image / ref</span>
            <span className="dcc-val"><code>{data.image}</code></span>
          </div>
        )}
        {data.domain && (
          <div className="dcc-row">
            <span className="dcc-label">Domain</span>
            <span className="dcc-val">{data.domain}</span>
          </div>
        )}
        {data.summary_points?.length > 0 && (
          <ul className="dcc-points">
            {data.summary_points.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        )}
      </div>
      <div className="dcc-footer">
        {submitted ? (
          <span className="dcc-submitting">Submitting…</span>
        ) : editing ? (
          <span className="dcc-edit-hint">Type your changes below and send them.</span>
        ) : (
          <>
            <button className="dcc-submit-btn" onClick={handleSubmit}>Submit to DevOps</button>
            <button className="dcc-edit-btn" onClick={() => { setEditing(true); onEdit?.(); }}>Make changes</button>
          </>
        )}
      </div>
    </div>
  );
}

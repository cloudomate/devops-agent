import { useState } from 'react';

function getPrimaryArg(args) {
  if (!args || !Object.keys(args).length) return null;
  return (
    args.project_slug ?? args.project ?? args.name ?? args.github_repo
    ?? args.environment ?? args.project_name ?? null
  );
}

const STATUS_DOT = {
  running: { color: '#d29922', label: 'running' },
  done:    { color: '#3fb950', label: 'done'    },
  error:   { color: '#f85149', label: 'error'   },
};

export default function ToolChip({ name, args, result, status }) {
  const [open, setOpen] = useState(false);
  const dot = STATUS_DOT[status] || STATUS_DOT.running;
  const primary = getPrimaryArg(args);

  return (
    <div style={{ margin: '2px 0' }}>
      {/* Chip row */}
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '7px',
          background: 'none',
          border: 'none',
          borderRadius: '20px',
          padding: '3px 10px 3px 8px',
          cursor: 'pointer',
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: '0.76rem',
          color: '#8b949e',
          userSelect: 'none',
          maxWidth: '100%',
        }}
      >
        {/* Tool name */}
        <span style={{ color: dot.color, fontWeight: 600 }}>{name}</span>

        {/* Primary arg (dimmed) */}
        {primary && (
          <span style={{ color: '#6e7681', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 220 }}>
            {primary}
          </span>
        )}

        {/* Expand toggle */}
        <span style={{ color: '#8b949e', fontSize: '0.65rem', marginLeft: 2 }}>
          {open ? '▴' : '▾'}
        </span>
      </button>

      {/* Expanded detail */}
      {open && (
        <div style={{
          marginTop: 4,
          background: '#0d1117',
          border: '1px solid #21262d',
          borderRadius: 6,
          padding: '10px 12px',
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: '0.75rem',
          color: '#c9d1d9',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
          maxHeight: 280,
          overflowY: 'auto',
        }}>
          {/* Args */}
          {args && Object.keys(args).length > 0 && (
            <>
              <div style={{ color: '#8b949e', fontSize: '0.68rem', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.06em' }}>args</div>
              <div style={{ color: '#c9d1d9' }}>{JSON.stringify(args, null, 2)}</div>
            </>
          )}
          {/* Result */}
          {result !== undefined && (
            <>
              <div style={{ color: '#8b949e', fontSize: '0.68rem', marginTop: 10, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.06em' }}>result</div>
              <div style={{ color: status === 'error' ? '#f85149' : '#c9d1d9' }}>
                {typeof result === 'string' ? result : JSON.stringify(result, null, 2)}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

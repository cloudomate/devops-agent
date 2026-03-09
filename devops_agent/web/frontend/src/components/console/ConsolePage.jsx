import { useState, useRef, useEffect } from 'react';

export default function ConsolePage() {
  const [output, setOutput] = useState([]);
  const [aiLog, setAiLog] = useState([]);
  const [cmd, setCmd] = useState('');
  const [aiInput, setAiInput] = useState('');
  const [running, setRunning] = useState(false);
  const outputEndRef = useRef(null);
  const aiEndRef = useRef(null);

  function scrollOutput() { outputEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }
  function scrollAi() { aiEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }

  async function runCmd() {
    const c = cmd.trim();
    if (!c) return;
    setCmd('');
    setOutput(p => [...p, { type: 'cmd', text: `$ ${c}` }]);
    scrollOutput();
    try {
      const r = await fetch('/api/console/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: c }),
      });
      const data = await r.json();
      if (data.stdout) setOutput(p => [...p, { type: 'out', text: data.stdout }]);
      if (data.stderr) setOutput(p => [...p, { type: 'err', text: data.stderr }]);
      const rc = data.return_code ?? 0;
      setOutput(p => [...p, { type: rc === 0 ? 'rc-ok' : 'rc-err', text: `exit ${rc}` }]);
    } catch (e) {
      setOutput(p => [...p, { type: 'err', text: e.message }]);
    }
    scrollOutput();
  }

  async function askAI() {
    const q = aiInput.trim();
    if (!q) return;
    setAiInput('');
    setAiLog(p => [...p, { type: 'ai', text: `> ${q}` }]);
    // Pipe the last ~20 lines of output as context
    const ctx = output.slice(-20).map(l => l.text).join('\n');
    try {
      const r = await fetch('/api/console/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, context: ctx }),
      });
      const data = await r.json();
      setAiLog(p => [...p, { type: 'result', text: data.answer || data.error || 'No response' }]);
    } catch (e) {
      setAiLog(p => [...p, { type: 'err', text: e.message }]);
    }
    scrollAi();
  }

  return (
    <div className="page page-active" style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <header className="top-header">
        <h1>Console</h1>
        <span className="header-hint">Run kubectl / helm / git commands</span>
      </header>
      <div className="console-layout">

        {/* Terminal */}
        <div className="console-pane">
          <div className="console-toolbar">
            <span className="console-label">Terminal</span>
            <button className="btn-sm" onClick={() => setOutput([])}>Clear</button>
          </div>
          <div className="console-output">
            {output.map((l, i) => (
              <div key={i} className={`co-${l.type}`}>{l.text}</div>
            ))}
            <div ref={outputEndRef} />
          </div>
          <div className="console-input-row">
            <span className="console-prompt">$</span>
            <input
              type="text"
              className="console-input"
              placeholder="kubectl get pods -A"
              value={cmd}
              onChange={e => setCmd(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && runCmd()}
              autoComplete="off"
              spellCheck={false}
            />
            <button className="btn-sm btn-primary" onClick={runCmd}>Run</button>
          </div>
        </div>

        {/* AI Assistant */}
        <div className="console-pane">
          <div className="console-toolbar">
            <span className="console-label">Agent Log &amp; AI Assistant</span>
            <button className="btn-sm" onClick={() => setAiLog([])}>Clear</button>
          </div>
          <div className="console-output">
            {aiLog.map((l, i) => (
              <div key={i} className={`co-${l.type}`}>{l.text}</div>
            ))}
            <div ref={aiEndRef} />
          </div>
          <div className="console-input-row">
            <input
              type="text"
              className="console-input"
              placeholder="Ask AI about the output…"
              value={aiInput}
              onChange={e => setAiInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && askAI()}
              autoComplete="off"
            />
            <button className="btn-sm btn-primary" onClick={askAI}>Ask AI</button>
          </div>
        </div>

      </div>
    </div>
  );
}

import { useState, useEffect, useRef, useCallback } from 'react';
import { marked } from 'marked';
import ToolChip from './ToolChip.jsx';
import ConfirmCard from './ConfirmCard.jsx';
import DiscoverForm from './DiscoverForm.jsx';

marked.setOptions({ breaks: true, gfm: true });

function parseContent(text) {
  const CONFIRM_RE = /```deploy-confirm\n([\s\S]*?)```/;
  const DISCOVER_RE = /```discover-form\n([\s\S]*?)```/;

  let confirmData = null;
  let discoverData = null;
  let cleaned = text;

  const cm = text.match(CONFIRM_RE);
  if (cm) {
    try { confirmData = JSON.parse(cm[1]); } catch (_) {}
    cleaned = cleaned.replace(CONFIRM_RE, '');
  }

  const dm = text.match(DISCOVER_RE);
  if (dm) {
    try { discoverData = JSON.parse(dm[1]); } catch (_) {}
    cleaned = cleaned.replace(DISCOVER_RE, '');
  }

  return { html: marked.parse(cleaned), confirmData, discoverData };
}

// Session key is per-user + per-project so different users get different histories
function sessionKey(userId, projectName) {
  const u = userId || 'anon';
  return projectName ? `session_${u}_${projectName}` : `session_${u}_global`;
}

function getSession(userId, projectName) {
  const key = sessionKey(userId, projectName);
  let id = localStorage.getItem(key);
  if (!id) { id = crypto.randomUUID(); localStorage.setItem(key, id); }
  return id;
}

function clearSession(userId, projectName) {
  localStorage.removeItem(sessionKey(userId, projectName));
  return getSession(userId, projectName);
}

// ── Message component ────────────────────────────────────────────────────────
function Message({ item, onConfirmSubmit, onConfirmEdit, onDiscoverSubmit }) {
  if (item.type === 'tool') {
    return <ToolChip name={item.name} args={item.args} result={item.result} status={item.status} />;
  }
  if (item.role === 'user') {
    return (
      <div className="message user">
        <div className="role-label">You</div>
        <div className="bubble">{item.content}</div>
      </div>
    );
  }
  const { html, confirmData, discoverData } = parseContent(item.content || '');
  const hasText = html.replace(/<[^>]*>/g, '').trim().length > 0;
  if (!item.streaming && !hasText && !confirmData && !discoverData) return null;
  return (
    <div className="message assistant">
      {(hasText || item.streaming) && (
        <div className="bubble">
          <span dangerouslySetInnerHTML={{ __html: html }} />
          {item.streaming && <span className="cursor" />}
        </div>
      )}
      {confirmData && !item.streaming && (
        <ConfirmCard data={confirmData} onSubmit={onConfirmSubmit} onEdit={onConfirmEdit} />
      )}
      {discoverData && !item.streaming && (
        <DiscoverForm data={discoverData} onSubmit={onDiscoverSubmit} />
      )}
    </div>
  );
}

// ── ChatPage ─────────────────────────────────────────────────────────────────
export default function ChatPage({ user, activeProject, isDevops, onRefreshSidebar, autoSend }) {
  const [items, setItems] = useState([]);
  const [sending, setSending] = useState(false);
  const [input, setInput] = useState('');

  const wsRef            = useRef(null);
  const sessionIdRef     = useRef(null);
  const streamingIdRef   = useRef(null);
  const rawBufferRef     = useRef('');
  const pendingChipsRef  = useRef({});
  const bottomRef        = useRef(null);
  const inputRef         = useRef(null);
  const projectRef       = useRef(activeProject);

  const userId = user?.id || user?.username || 'anon';

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'auto' });
  }, []);

  // Re-init on project switch
  useEffect(() => {
    projectRef.current = activeProject;
    initSession();
    return () => {
      if (wsRef.current) {
        wsRef.current.onclose = null; // prevent stray reconnect on cleanup
        try { wsRef.current.close(); } catch (_) {}
      }
    };
  }, [activeProject]);

  // Auto-send a message when triggered externally (e.g. clicking a deployment request)
  useEffect(() => {
    if (!autoSend) return;
    // Delay slightly so WS is ready after project switch
    const t = setTimeout(() => doSend(autoSend), 800);
    return () => clearTimeout(t);
  }, [autoSend]);

  function initSession(forceNew = false) {
    if (forceNew) {
      sessionIdRef.current = clearSession(userId, activeProject);
    } else {
      sessionIdRef.current = getSession(userId, activeProject);
    }
    streamingIdRef.current = null;
    rawBufferRef.current   = '';
    pendingChipsRef.current = {};
    setItems([]);
    setSending(false);

    if (!forceNew) loadHistory(sessionIdRef.current);
    connectWS();
  }

  async function loadHistory(sessionId) {
    try {
      const msgs = await fetch(`/api/chat-history/${sessionId}`).then(r => r.ok ? r.json() : []);
      setItems(msgs.map(m => {
        let content = m.content;
        // Replace raw YAML form payload with a clean summary in the chat bubble
        if (m.role === 'user' && content.startsWith('[ONBOARDING FORM RESPONSE]')) {
          const envMatch = content.match(/environments:\s*\[([^\]]*)\]/);
          const varCount = (content.match(/^\s+\w+:/gm) || []).length;
          const envs = envMatch ? envMatch[1] : '?';
          content = `Onboarding form submitted — ${envs} · ${varCount} env vars`;
        }
        return {
          id: crypto.randomUUID(),
          type: 'msg',
          role: m.role === 'user' ? 'user' : 'assistant',
          content,
          streaming: false,
        };
      }));
      setTimeout(scrollToBottom, 50);
    } catch (_) {}
  }

  function connectWS() {
    if (wsRef.current) {
      wsRef.current.onclose = null; // intentional close — don't trigger reconnect loop
      try { wsRef.current.close(); } catch (_) {}
    }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/chat/${sessionIdRef.current}`);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'ping') return; // keepalive, ignore

      if (data.type === 'delta') {
        rawBufferRef.current += data.text;
        const content = rawBufferRef.current;
        if (!streamingIdRef.current) {
          const id = crypto.randomUUID();
          streamingIdRef.current = id;
          setItems(prev => [...prev, { id, type: 'msg', role: 'assistant', content, streaming: true }]);
        } else {
          const sid = streamingIdRef.current;
          setItems(prev => prev.map(it => it.id === sid ? { ...it, content } : it));
        }
        scrollToBottom();
      }

      else if (data.type === 'tool_start') {
        finalizeStreaming();
        const id = crypto.randomUUID();
        pendingChipsRef.current[data.name] = id;
        setItems(prev => [...prev, { id, type: 'tool', name: data.name, args: data.args, status: 'running' }]);
        scrollToBottom();
      }

      else if (data.type === 'tool_done') {
        const chipId = pendingChipsRef.current[data.name];
        if (chipId) {
          delete pendingChipsRef.current[data.name];
          const isError = typeof data.result === 'string' && /^error/i.test(data.result);
          setItems(prev => prev.map(it =>
            it.id === chipId ? { ...it, status: isError ? 'error' : 'done', result: data.result } : it
          ));
        }
        scrollToBottom();
      }

      else if (data.type === 'done') {
        finalizeStreaming();
        setSending(false);
        onRefreshSidebar?.();
        scrollToBottom();
      }
    };

    ws.onclose = () => setTimeout(connectWS, 2000);
  }

  function finalizeStreaming() {
    if (streamingIdRef.current) {
      const sid = streamingIdRef.current;
      const finalContent = rawBufferRef.current; // capture before resetting refs
      streamingIdRef.current = null;
      rawBufferRef.current   = '';
      setItems(prev => prev.map(it =>
        it.id === sid ? { ...it, content: finalContent, streaming: false } : it
      ));
    }
  }

  function doSend(text, display = null) {
    if (!text || sending) return;
    setItems(prev => [...prev, { id: crypto.randomUUID(), type: 'msg', role: 'user', content: display || text }]);
    setInput('');
    if (inputRef.current) inputRef.current.style.height = 'auto';
    setSending(true);
    wsRef.current?.send(JSON.stringify({ message: text, project: projectRef.current }));
    scrollToBottom();
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(input.trim()); }
  }

  function handleInputChange(e) {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }

  const persona = isDevops ? 'devops' : 'developer';

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, overflow: 'hidden' }}>

      {/* Header */}
      <header className="top-header" style={{ flexShrink: 0 }}>
        <div className="header-title-row">
          <h1>
            DevOps Agent
            <span className={`persona-badge ${persona}`} style={{ marginLeft: 10 }}>
              {isDevops ? 'DevOps' : 'Developer'}
            </span>
          </h1>
          {activeProject && <span className="chat-project-label">/ {activeProject}</span>}
        </div>
        <span className="header-hint" style={{ flex: 1 }}>
          {activeProject
            ? `Project: ${activeProject}`
            : 'Select a project from the sidebar'}
        </span>
        <div className="header-actions">
          <button
            className="new-chat-btn"
            onClick={() => initSession(true)}
            title="Clear history and start a new chat"
          >
            + New Chat
          </button>
        </div>
      </header>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
        {items.length === 0 && !sending && (
          <div style={{ margin: 'auto', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.875rem' }}>
            {activeProject
              ? `Chat about ${activeProject} — ask to onboard, deploy, or review`
              : 'Select a project from the sidebar to begin'}
          </div>
        )}
        {/* Group consecutive agent/tool items into a timeline thread */}
        {(() => {
          const groups = [];
          let thread = null;
          for (const item of items) {
            if (item.role === 'user') {
              thread = null;
              groups.push({ type: 'user', item });
            } else {
              if (!thread) { thread = { type: 'thread', key: item.id, items: [] }; groups.push(thread); }
              thread.items.push(item);
            }
          }
          const msgProps = { onConfirmSubmit: () => doSend('__SUBMIT_CONFIRMED__'), onConfirmEdit: () => inputRef.current?.focus(), onDiscoverSubmit: (msg, display) => doSend(msg, display) };
          return groups.map(g => {
            if (g.type === 'user') return <Message key={g.item.id} item={g.item} {...msgProps} />;
            return (
              <div key={g.key} className="agent-thread">
                {g.items.map(item => (
                  item.type === 'tool'
                    ? <div key={item.id} className="tool-entry"><Message item={item} {...msgProps} /></div>
                    : <Message key={item.id} item={item} {...msgProps} />
                ))}
              </div>
            );
          });
        })()}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="input-row" style={{ flexShrink: 0 }}>
        <textarea
          ref={inputRef}
          id="input"
          rows={1}
          placeholder={activeProject ? 'Ask the agent something…' : 'Select a project first…'}
          value={input}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          disabled={sending}
          autoFocus
        />
        <button
          id="send-btn"
          onClick={() => doSend(input.trim())}
          disabled={sending || !input.trim()}
        >
          {sending ? '…' : 'Send'}
        </button>
      </div>

    </div>
  );
}

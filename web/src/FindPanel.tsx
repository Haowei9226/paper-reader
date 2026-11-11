import {useEffect, useRef, useState} from 'react';
import {Search} from 'lucide-react';
import {api} from './api';
import {Busy, ErrorLine} from './components';
import {readableText} from './HtmlView';
import type {FindHit, Paper} from './types';

type SearchResponse = {items: FindHit[]; next_offset: number | null};
const hitKey = (hit: FindHit) => `${hit.translation_id ?? hit.block_id}-${hit.side}-${hit.start}-${hit.end}`;

export default function FindPanel({paper, focus, revision, activeHit, onHit, onClear}: {
  paper: Paper; focus: number; revision: string; activeHit?: FindHit; onHit: (hit: FindHit) => void; onClear: () => void;
}) {
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState('both');
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const pending = useRef<AbortController>(undefined);
  const hasText = paper.blocks_count > 0;
  const active = activeHit && hitKey(activeHit);

  useEffect(() => {input.current?.focus(); input.current?.select()}, [focus]);

  async function load(offset = 0) {
    pending.current?.abort();
    const request = new AbortController();
    pending.current = request;
    setBusy(true);
    setError('');
    try {
      const params = new URLSearchParams({q: query.trim(), scope, offset: String(offset)});
      const value = await api<SearchResponse>(`/papers/${paper.id}/find?${params}`, {signal: request.signal});
      if (!request.signal.aborted) setResult(previous => ({...value, items: offset ? [...(previous?.items ?? []), ...value.items] : value.items}));
    } catch (error) {
      if (!request.signal.aborted) setError((error as Error).message);
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    setBusy(false);
    if (!query.trim() || !hasText) return;
    const timer = window.setTimeout(() => void load(), 180);
    return () => {window.clearTimeout(timer); pending.current?.abort()};
  }, [paper.id, query, scope, hasText, revision]);

  function changeSearch() {
    setResult(null);
    setError('');
    onClear();
  }

  function step(backward: boolean) {
    const items = result?.items ?? [];
    if (!items.length) return;
    const index = items.findIndex(hit => hitKey(hit) === active);
    const next = items[index < 0 ? (backward ? items.length - 1 : 0) : (index + (backward ? -1 : 1) + items.length) % items.length];
    onHit(next);
    document.getElementById('find-' + hitKey(next))?.scrollIntoView({block: 'nearest'});
  }

  return <div className="find-panel">
    <div className="find-controls">
      <label className="find-field"><Search size={16}/><input
        ref={input} type="search" aria-label="文内查找" placeholder="输入关键词或短语" maxLength={200} value={query}
        onChange={event => {setQuery(event.target.value); changeSearch()}}
        onKeyDown={event => {if (event.key === 'Enter' && !event.nativeEvent.isComposing) {event.preventDefault(); step(event.shiftKey)}}}
      /></label>
      <label className="find-scope"><span>查找范围</span><select value={scope} onChange={event => {setScope(event.target.value); changeSearch()}}>
        <option value="both">原文和译文</option><option value="source">仅原文</option><option value="translation">仅译文</option>
      </select></label>
    </div>
    {!hasText ? <p className="muted small">{paper.parse_state === 'no_text' ? '此 PDF 没有可查找的文字。' : '正文解析后即可查找。'}</p>
      : !query.trim() ? <p className="muted small">查找全文和已有译文。按 Enter 跳到下一处。</p>
      : <>
        <ErrorLine text={error}/>
        {busy && !result && <Busy text="正在查找"/>}
        {result && <>
          <p className="muted small find-count" role="status">{result.items.length ? `${result.next_offset === null ? '找到' : '已显示'} ${result.items.length} 处` : '没有找到匹配的文字'}</p>
          <div className="find-results">{result.items.map(hit => {
            const key = hitKey(hit);
            return <button className="find-result" key={key} id={'find-' + key} aria-current={active === key ? 'true' : undefined} onClick={() => onHit(hit)}>
              <span className="find-result-meta"><span>{hit.side === 'source' ? '原文' : '译文'}</span><span>第 {hit.page} 页</span></span>
              <span className="find-snippet">{hit.before}<mark>{readableText(hit.match)}</mark>{hit.after}</span>
            </button>;
          })}</div>
          {result.next_offset !== null && <button className="button compact find-more" disabled={busy} onClick={() => void load(result.next_offset!)}>{busy ? '正在查找…' : '显示更多'}</button>}
        </>}
      </>}
  </div>;
}

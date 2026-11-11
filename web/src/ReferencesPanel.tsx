import {useLayoutEffect,useRef} from 'react';
import {ArrowUpRight,BookOpen} from 'lucide-react';
import {Busy,ErrorLine} from './components';
import type {ReferenceAnchor,ReferenceIndex} from './types';
import {referenceLabel} from './types';

export type ReferenceFocus = {numbers:number[];active:number;request:number};

export default function ReferencesPanel({index,error,focus,navigating,onFocus,onJump,onOpenPaper}: {
  index:ReferenceIndex|null;
  error:string;
  focus:ReferenceFocus|null;
  navigating:boolean;
  onFocus:(number:number)=>void;
  onJump:(anchor:ReferenceAnchor)=>void;
  onOpenPaper:(id:string,number:number)=>void;
}) {
  const activeEntry = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (!focus) return;
    activeEntry.current?.scrollIntoView({block:'nearest'});
    activeEntry.current?.focus({preventScroll:true});
  }, [focus,index]);

  return <section className="reference-panel" aria-label="参考文献">
    <ErrorLine text={error}/>
    {!index && !error && <Busy text="正在提取参考文献"/>}
    {index && !index.entries.length && <>
      <p className="muted small">{index.heading ? '此文献没有识别到可跳转的参考条目。' : '此文献没有识别到参考文献小节。'}</p>
      {index.heading && <button className="button compact" onClick={() => onJump(index.heading!)}>查看原文参考文献</button>}
    </>}
    {!!index?.entries.length && <>
      <div className="reference-panel-top">
        <div className="reference-panel-heading">
          <span>参考文献</span>
          <span className="muted">{index.entries.length} 条</span>
        </div>
        {focus && focus.numbers.length > 1 && <div className="reference-group" aria-label="本处引用">
          <span>本处引用</span>
          {focus.numbers.map(number => {
            const entry = index.entries.find(entry=>entry.number===number);
            const label = entry ? referenceLabel(entry) : '参考条目';
            return <button key={number} title={label} aria-pressed={focus.active === number} onClick={() => onFocus(number)}>{label}</button>;
          })}
        </div>}
      </div>
      <ol className="reference-list">
        {index.entries.map(entry => {
          const selected = focus?.active === entry.number;
          const label = referenceLabel(entry);
          const libraryMatch = entry.library_match;
          const search = entry.url_kind==='search' || !entry.url;
          const externalUrl = entry.url || 'https://scholar.google.com/scholar?q='+encodeURIComponent(entry.text);
          const externalLabel = search ? '搜索文献' : '浏览器打开';
          const externalHint = search ? '在浏览器中搜索文献' : '在浏览器中打开文献页面';
          return <li key={entry.number}>
            <article className="reference-entry" data-reference-number={entry.number} aria-current={selected ? 'true' : undefined} aria-label={`参考文献 ${label}`}
              tabIndex={-1} ref={selected ? activeEntry : undefined}>
              <div className="reference-entry-header">
                <span className="reference-number">{label}</span>
                <button className="text-button" onClick={() => onJump(entry.anchor)}>原文第 {entry.anchor.page} 页</button>
              </div>
              {libraryMatch ? <button className="reference-title" type="button" disabled={navigating}
                aria-label={`打开库内文献 ${libraryMatch.title}`}
                title="打开库内文献，可返回当前阅读位置"
                onClick={() => onOpenPaper(libraryMatch.paper_id,entry.number)}>{entry.text}</button>
                : <a className="reference-title" href={externalUrl} target="_blank" rel="noopener noreferrer"
                  title={externalHint} onClick={() => onFocus(entry.number)}>{entry.text}</a>}
              <div className="reference-actions">
                {libraryMatch && <button className="reference-doi" type="button" disabled={navigating}
                  title={libraryMatch.title} onClick={() => onOpenPaper(libraryMatch.paper_id,entry.number)}>
                  <BookOpen size={13}/>打开库内文献
                </button>}
                <a className="reference-doi" href={externalUrl} target="_blank" rel="noopener noreferrer" title={externalHint}>
                  {externalLabel} <ArrowUpRight size={13}/>
                </a>
              </div>
              {libraryMatch && entry.library_match_method==='title_author_year' &&
                <p className="reference-match-hint">按标题、首位作者和年份匹配</p>}
            </article>
          </li>;
        })}
      </ol>
    </>}
  </section>;
}

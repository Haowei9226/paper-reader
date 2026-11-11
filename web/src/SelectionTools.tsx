import {useEffect, useState} from 'react';
import {ArrowUpRight, Highlighter, Languages, MessageSquare, Sparkles} from 'lucide-react';
import {api} from './api';
import {readableText} from './HtmlView';
import type {Counterpart, CounterpartRange, Selection} from './types';

export function useCounterpart(paperId: string, selection: Selection | null, pdf: boolean) {
  const [loaded, setLoaded] = useState<{selection:Selection;data?:Counterpart;error?:string}>();
  useEffect(() => {
    if (!selection) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const targets = selection.side === 'source' ? selection.targets : selection.translated_targets;
      void api<Counterpart>(`/papers/${paperId}/counterpart`, {
        method: 'POST', signal: controller.signal,
        body: JSON.stringify({side:selection.side,targets:targets.slice(0,50),pdf}),
      }).then(data => {if (!controller.signal.aborted) setLoaded({selection,data})})
        .catch(error => {if (!controller.signal.aborted) setLoaded({selection,error:error.message})});
    }, 65);
    return () => {window.clearTimeout(timer);controller.abort()};
  }, [paperId, selection, pdf]);
  return loaded?.selection === selection ? loaded : undefined;
}

export default function SelectionTools({selection, counterpart, error, onHighlight, onNote, onTranslate, onExplain, onLocate}: {
  selection:Selection;counterpart?:Counterpart;error?:string;onHighlight:()=>void;onNote:()=>void;onTranslate:()=>void;onExplain:()=>void;
  onLocate:(range:CounterpartRange)=>void;
}) {
  const title = selection.side === 'source' ? '对应译文' : '对应原文';
  const first = counterpart?.ranges[0];
  const multiple = (counterpart?.ranges.length ?? 0) > 1;
  const width = Math.min(370, window.innerWidth - 24);
  const x = Math.max(12, Math.min(selection.x, window.innerWidth - width - 12));
  const reserve = first ? 208 : 105;
  const above = selection.y + reserve > window.innerHeight && selection.top >= reserve + 8;
  const y = above ? selection.top - 8 : Math.max(12, Math.min(selection.y, window.innerHeight - reserve));
  const label = counterpart?.ranges.some(range=>range.quality==='paragraph') ? '整段参考'
    : counterpart?.ranges.every(range=>range.quality==='manual') ? '人工校正' : '对应句段';
  return <div className="selection-tools" style={{left:x,top:y,width,transform:above?'translateY(-100%)':undefined}} onMouseDown={event=>event.preventDefault()}>
    <div className="selection-toolbar" role="toolbar" aria-label="选中文字操作">
      <button title="高亮所选文字" onClick={onHighlight}><Highlighter size={16}/><span>高亮</span></button>
      <button onClick={onNote}><MessageSquare size={16}/><span>笔记</span></button>
      <button onClick={onTranslate}><Languages size={16}/><span>翻译</span></button>
      <button onClick={onExplain}><Sparkles size={16}/><span>解释</span></button>
    </div>
    <div className="selection-preview" aria-live="polite">
      <div className="selection-preview-heading">
        <strong>{title}</strong>
        {first && <><span>{label}</span>{!multiple && <button className="text-button" onClick={()=>onLocate(first)} aria-label={`定位${title}`}>
          <ArrowUpRight size={13}/>定位到第 {first.page} 页
        </button>}</>}
      </div>
      {first ? <div className="selection-preview-text">
        {counterpart.ranges.map((range,index)=><p key={`${range.block_id}:${range.start}:${index}`}>
          {multiple && <button type="button" className="text-button selection-preview-page" onClick={()=>onLocate(range)}
            aria-label={`定位${title}第 ${range.page} 页`}>第 {range.page} 页<ArrowUpRight size={11}/></button>}
          {readableText(range.quote)}
        </p>)}
        {!!counterpart.missing&&<small>部分选段还没有对应文字。</small>}
      </div> : <p className="selection-preview-empty">{error || (!counterpart ? '正在查找…' : selection.side==='source' ? '这一段还没有译文。' : '没有找到可对照的原文。')}</p>}
    </div>
  </div>;
}

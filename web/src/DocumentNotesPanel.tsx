import {useLayoutEffect,useRef} from 'react';
import {ArrowUpRight,BookOpen} from 'lucide-react';
import {Busy,ErrorLine} from './components';
import type {DocumentNotesIndex,ReferenceAnchor} from './types';

export type DocumentNoteFocus = {ids:string[];active:string;request:number};

export default function DocumentNotesPanel({index,error,focus,navigating,canReturn,onFocus,onJump,onOpenPaper,onReturn}: {
  index:DocumentNotesIndex|null;
  error:string;
  focus:DocumentNoteFocus|null;
  navigating:boolean;
  canReturn:boolean;
  onFocus:(id:string)=>void;
  onJump:(anchor:ReferenceAnchor)=>void;
  onOpenPaper:(paperId:string,noteId:string)=>void;
  onReturn:()=>void;
}) {
  const active = useRef<HTMLElement>(null);
  useLayoutEffect(()=>{
    if (!focus) return;
    active.current?.scrollIntoView({block:'nearest'});
    active.current?.focus({preventScroll:true});
  },[index,focus]);
  const label = (id:string) => {
    const note = index?.entries.find(note=>note.id===id);
    return note ? `${note.kind==='footnote'?'脚注':'尾注'} ${note.label} · 第 ${note.anchor.page} 页` : '注释';
  };
  return <section className="reference-panel" aria-label="脚注和尾注">
    <ErrorLine text={error}/>
    {!index&&!error&&<Busy text="正在提取脚注和尾注"/>}
    {index&&!index.entries.length&&<p className="small muted">此文献没有识别到脚注或尾注。</p>}
    {!!index?.entries.length&&<>
      <div className="reference-panel-top">
        <div className="reference-panel-heading"><span>脚注和尾注</span><span className="muted">{index.entries.length} 条</span></div>
        {canReturn&&<button className="text-button note-return" onClick={onReturn}>返回正文阅读位置</button>}
        {focus&&focus.ids.length>1&&<div className="reference-group" aria-label="本处注释">
          {focus.ids.map(id=><button key={id} title={label(id)} aria-pressed={focus.active===id} onClick={()=>onFocus(id)}>{label(id)}</button>)}
        </div>}
      </div>
      <ol className="reference-list">
        {index.entries.map(note=><li key={note.id}>
          <article className="reference-entry document-note-entry" data-note-id={note.id}
            aria-label={label(note.id)} aria-current={focus?.active===note.id?true:undefined}
            tabIndex={-1} ref={focus?.active===note.id?active:undefined}>
            <div className="reference-entry-header">
              <span className="reference-number">{note.kind==='footnote'?'脚注':'尾注'} {note.label}</span>
              <button className="text-button" onClick={()=>onJump(note.anchor)}>原文第 {note.anchor.page} 页</button>
            </div>
            <p className="document-note-text">{note.text}</p>
            {note.references.map((reference,index)=>{
              const search = reference.url_kind==='search';
              const match = reference.library_match;
              return <div className="note-reference" key={index}>
                {(note.references.length>1||reference.text.trim()!==note.text.trim())&&<p>{reference.text}</p>}
                <div className="reference-actions">
                  {match&&<button className="reference-doi" type="button" disabled={navigating}
                    title={match.title} onClick={()=>onOpenPaper(match.paper_id,note.id)}><BookOpen size={13}/>打开库内文献</button>}
                  <a className="reference-doi" href={reference.url} target="_blank" rel="noopener noreferrer">
                    {search?'搜索文献':'浏览器打开'}<ArrowUpRight size={13}/>
                  </a>
                </div>
              </div>;
            })}
          </article>
        </li>)}
      </ol>
    </>}
  </section>;
}

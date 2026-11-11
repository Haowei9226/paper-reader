import {useEffect,useRef,useState} from 'react';
import {ArrowUpRight,BookOpen,Check,ChevronRight,Download,FileText,Highlighter,Link2,LoaderCircle,MessageSquare,MoreHorizontal,Pencil,Plus,Search,Send,Sparkles,Star,Trash2,Undo2} from 'lucide-react';
import {api,date,patch,post,remove} from './api';
import {Busy,Empty,ErrorLine,IconButton} from './components';
import {PROVIDERS,PROVIDER_NAMES} from './types';
import type {AIResult,Annotation,Bootstrap,Job,Paper,Provider} from './types';
import {COLOR_LABELS,PUBLICATION_STATUS_LABELS,STATUS_LABELS} from './types';

export function InfoPanel({paper,bootstrap,onEdit,onChange,onOpen,onDeleted,onPurge,onNotes}:{paper:Paper|null;bootstrap:Bootstrap;onEdit:()=>void;onChange:()=>void;onOpen:()=>void;onDeleted:()=>void;onPurge?:()=>void;onNotes?:()=>void}){
  const [error,setError]=useState(''),[metadataBusy,setMetadataBusy]=useState(false);
  if(!paper)return <div className="detail-placeholder"><FileText size={29}/><p>选择一篇文献</p><span>查看信息、标签与阅读记录</span></div>;
  const change=async(value:unknown)=>{try{await patch('/papers/'+paper.id,value);onChange()}catch(e){setError((e as Error).message)}};
  const bibliographicAction=async(action:'metadata'|'metadata/authors'|'rename')=>{
    setError('');setMetadataBusy(true);
    try{await post('/papers/'+paper.id+'/'+action);onChange()}catch(e){setError((e as Error).message)}finally{setMetadataBusy(false)}
  };
  const metadataLabel=paper.metadata_status==='matched'?'已匹配 · '+paper.metadata_source:
    paper.metadata_status==='queued'?'查找已排队':paper.metadata_status==='pending'?'等待读取 PDF':
    paper.metadata_status==='review'?'未确认匹配，可编辑 DOI 后重查':paper.metadata_status==='offline'?'暂未连上书目服务':
    paper.metadata_source?'信息来自 '+paper.metadata_source:'可联网补全信息';
  async function trash(){try{await remove('/papers/'+paper!.id);onDeleted()}catch(e){setError((e as Error).message)}}
  return <div className="info-panel"><div className="detail-document-icon"><FileText size={28}/><span>PDF</span></div><div className="detail-title-row"><h2>{paper.title}</h2><IconButton label={paper.starred?'取消星标':'添加星标'} active={!!paper.starred} onClick={()=>change({starred:!paper.starred})}><Star size={18} fill={paper.starred?'currentColor':'none'}/></IconButton></div><p className="detail-authors">{paper.authors||'作者信息待补充'}</p><div className="detail-main-actions">{paper.deleted_at?<button className="button primary" onClick={async()=>{try{await post('/papers/'+paper.id+'/restore');onChange()}catch(e){setError((e as Error).message)}}}><Undo2 size={15}/>恢复文献</button>:<button className="button primary" onClick={onOpen}><BookOpen size={15}/>开始阅读</button>}<button className="button" onClick={onEdit}><Pencil size={15}/>编辑</button></div><ErrorLine text={error}/>
  <dl className="metadata"><div><dt>发表状态</dt><dd>
    <select aria-label="发表状态" value={paper.publication_status??''} onChange={e=>change({publication_status:e.target.value})}>
      {Object.entries(PUBLICATION_STATUS_LABELS).map(([key,label])=><option key={key} value={key}>{label}</option>)}
    </select>
  </dd></div></dl>
  {['unpublished','internal'].includes(paper.publication_status)&&<p className="small muted">自动书目补全会跳过此文献。主动点击“联网补全”会用标题或 DOI 查询公开书目。</p>}
  <dl className="metadata"><div><dt>阅读状态</dt><dd><select value={paper.status} onChange={e=>change({status:e.target.value})}>{Object.entries(STATUS_LABELS).map(([k,v])=><option value={k} key={k}>{v}</option>)}</select></dd></div><div><dt>年份</dt><dd>{paper.year||'—'}</dd></div><div><dt>来源</dt><dd>{paper.source||'—'}</dd></div><div><dt>页数</dt><dd>{paper.pages?`${paper.pages} 页`:'解析中'}</dd></div><div><dt>原文语言</dt><dd>{paper.language==='zh'?'中文':'英文'}</dd></div>{paper.doi&&<div><dt>DOI</dt><dd><a href={'https://doi.org/'+encodeURIComponent(paper.doi.replace(/^https?:\/\/doi.org\//,''))} target="_blank" rel="noreferrer">{paper.doi}<ArrowUpRight size={12}/></a></dd></div>}</dl>
  <section className="detail-section"><div className="section-label"><h3>书目信息</h3>{!paper.deleted_at&&<div className="bibliography-actions">
    <button className="text-button" disabled={metadataBusy||paper.parse_state!=='ready'||paper.metadata_status==='queued'} onClick={()=>bibliographicAction('metadata/authors')}>重读署名</button>
    <button className="text-button" disabled={metadataBusy} onClick={()=>bibliographicAction('metadata')}><Search size={13}/>联网补全</button>
  </div>}</div><p className="small muted" aria-live="polite">{metadataLabel}</p></section>
  <section className="detail-section"><div className="section-label"><h3>所属集合</h3><button className="text-button" onClick={onEdit}>管理</button></div><div className="detail-collections">{paper.collections.length?paper.collections.map(id=><span key={id}><span className="tiny-folder"/>{bootstrap.collections.find(c=>c.id===id)?.name||'集合'}</span>):<span className="muted small">尚未加入集合</span>}</div></section>
  <section className="detail-section"><div className="section-label"><h3>标签</h3><button className="text-button" onClick={onEdit}>添加</button></div><div className="tags">{paper.tags.length?paper.tags.map(t=><span className="tag" key={t}>{t}</span>):<span className="muted small">用标签串联研究主题</span>}</div></section>
  {paper.abstract&&<section className="detail-section"><h3>摘要</h3><p className="abstract-text">{paper.abstract}</p></section>}
  <section className="detail-section"><div className="section-label"><h3>阅读记录</h3>{onNotes&&!paper.deleted_at&&<button className="text-button" onClick={onNotes}>本篇笔记<ChevronRight size={13}/></button>}</div><div className="reading-stats"><span><Highlighter size={15}/>{paper.annotations_count} 条笔记与标注</span><span><FileText size={15}/>{paper.translated_count}/{paper.blocks_count} 段已翻译</span></div>{paper.locator.page&&<p className="small muted">上次阅读至第 {paper.locator.page} 页</p>}</section>
  <section className="detail-section"><div className="section-label"><h3>文件与导出</h3>{!paper.deleted_at&&<button className="text-button" disabled={metadataBusy} onClick={()=>bibliographicAction('rename')}>按书目信息命名</button>}</div><a className="file-link" href={`/api/papers/${paper.id}/file`} target="_blank" rel="noreferrer"><FileText size={17}/><span title={paper.filename}>{paper.filename}</span><ArrowUpRight size={14}/></a>{paper.original_filename&&paper.filename!==paper.original_filename&&<p className="small muted original-name" title={paper.original_filename}>原导入名：{paper.original_filename}</p>}<div className="export-links"><a href={`/api/papers/${paper.id}/file`} download={paper.filename}><Download size={14}/>原始 PDF</a><a href={`/api/papers/${paper.id}/export/pdf`}><Download size={14}/>带批注 PDF</a><a href={`/api/papers/${paper.id}/export/notes`}><Download size={14}/>阅读笔记</a><a href={`/api/papers/${paper.id}/export/html`}><Download size={14}/>双语 HTML</a></div></section>
  {paper.parse_error&&<p className="small parse-warning">{paper.parse_error}</p>}<div className="detail-bottom"><span>添加于 {date(paper.created)}</span>
    {paper.deleted_at ? onPurge && <button className="button danger" onClick={onPurge}><Trash2 size={13}/>永久删除</button> :
      <button className="text-button muted" onClick={trash}><Trash2 size={13}/>移入回收站</button>}
  </div></div>
}

type AnnotationActions = {
  pages: number;
  onChange: () => void;
  onOpenAnnotation: (annotation: Annotation) => void;
  onAlign?: (annotation: Annotation) => void;
  onAIAlign?: (annotation: Annotation) => void;
};

const alignmentLabels: Record<string, string> = {
  review: '待校对对应',
  manual: '已人工对应',
  suggested: 'AI 对应 · 可校对',
};

function originalTarget(annotation: Annotation, pages: number) {
  return annotation.targets.find(target => target.page && target.page >= 1 && target.page <= pages);
}

function AnnotationCard({annotation, selected, showUpdated, pages, onChange, onOpenAnnotation, onAlign, onAIAlign}:
  AnnotationActions & {annotation: Annotation; selected: boolean; showUpdated: boolean}) {
  const card = useRef<HTMLElement>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const {id, targets, translated_targets, quote, note, color, alignment, created, updated} = annotation;
  const source = originalTarget(annotation, pages);
  const hasExcerpt = targets.length > 0 || translated_targets.length > 0 || !!quote;
  const label = hasExcerpt ? note.trim() ? '摘录笔记' : '高亮' : '独立笔记';
  const alignmentLabel = alignmentLabels[alignment];

  useEffect(() => {
    if (selected) card.current?.scrollIntoView({block: 'nearest'});
  }, [selected]);

  async function change(save: () => Promise<unknown>, done?: () => void) {
    setBusy(true);
    setError('');
    try {
      await save();
      done?.();
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <article ref={card} data-note-id={id} className={`note-card ${selected ? 'selected' : ''}`}>
    <div className="note-meta">
      <div className="note-origin">
        <span>{label}</span>
        {source ? <button className="note-location" onClick={() => onOpenAnnotation(annotation)}>
          第 {source.page} 页<ChevronRight size={12}/>
        </button> : hasExcerpt && <span>原文位置不可用</span>}
      </div>
      <span title={showUpdated ? '最近更新' : '创建时间'}>{date(showUpdated ? updated : created)}</span>
    </div>
    {quote && <blockquote className={'note-quote '+color}>{quote}</blockquote>}
    {translated_targets.length > 0 && <p className="note-translation"><span>译文摘录</span>{translated_targets.map(target => target.quote).join('\n')}</p>}
    {draft !== null ? <>
      <textarea className="note-editor" aria-label="编辑笔记" rows={5} value={draft} disabled={busy}
        onChange={event => setDraft(event.target.value)}/>
      <div className="inline-actions">
        <button className="text-button" disabled={busy || (!hasExcerpt && !draft.trim())}
          onClick={() => void change(() => patch('/annotations/'+id, {note: draft}), () => setDraft(null))}>保存笔记</button>
        <button className="text-button muted" disabled={busy} onClick={() => setDraft(null)}>取消</button>
      </div>
    </> : note ? <p className="note-body">{note}</p> :
      <button className="note-add-text" disabled={busy} onClick={() => setDraft('')}>添加你的想法…</button>}
    {alignmentLabel && <div className="note-alignment">
      <span className={alignment==='review'?'alignment-review':''}>{alignmentLabel}</span>
      {source && onAlign && <button className="text-button" disabled={busy} onClick={() => onAlign(annotation)}>修正对应</button>}
    </div>}
    <div className="note-actions">
      <div className="color-options">
        {Object.entries(COLOR_LABELS).map(([value, name]) => <button key={value}
          className={`color-dot small-dot ${value} ${color===value?'chosen':''}`} title={name} aria-label={'改为'+name}
          aria-pressed={color === value} disabled={busy || color === value}
          onClick={() => void change(() => patch('/annotations/'+id, {color: value}))}/>)}
      </div>
      <div>
        <IconButton label="编辑笔记" disabled={busy || draft !== null} onClick={() => setDraft(note)}><Pencil size={14}/></IconButton>
        {source && targets.length === 1 && onAIAlign && <IconButton label="用当前模型查找对应短语" disabled={busy}
          onClick={() => onAIAlign(annotation)}><Link2 size={14}/></IconButton>}
        <IconButton label={hasExcerpt ? '删除这条标注和笔记' : '删除笔记'} disabled={busy}
          onClick={() => void change(() => remove('/annotations/'+id))}><Trash2 size={14}/></IconButton>
      </div>
    </div>
    <ErrorLine text={error}/>
  </article>;
}

export function AnnotationList({annotations, selectedId, showUpdated = false, ...actions}:
  AnnotationActions & {annotations: Annotation[]; selectedId?: string | null; showUpdated?: boolean}) {
  return annotations.map(annotation => <AnnotationCard key={annotation.id} annotation={annotation}
    selected={selectedId === annotation.id} showUpdated={showUpdated} {...actions}/>);
}

export function NotesPanel({paper, annotations, selectedId, onChange, onNew, onOpenAnnotation, onAlign, onAIAlign}: {
  paper: Paper;
  annotations: Annotation[];
  selectedId: string | null;
  onChange: () => void;
  onNew: () => void;
  onOpenAnnotation: (annotation: Annotation) => void;
  onAlign: (annotation: Annotation) => void;
  onAIAlign: (annotation: Annotation) => void;
}) {
  const [query, setQuery] = useState('');
  useEffect(() => {
    if (selectedId) setQuery('');
  }, [selectedId]);
  const keyword = query.trim().toLowerCase();
  const filtered = keyword ? annotations.filter(a =>
    [a.note, a.quote, ...a.translated_targets.map(target => target.quote)].some(value => value?.toLowerCase().includes(keyword))) : annotations;
  return <div className="notes-panel">
    <div className="panel-tools"><span>{annotations.length} 条阅读记录</span><button className="text-button" onClick={onNew}><Plus size={14}/>新笔记</button></div>
    {annotations.length > 0 && <div className="search-field small-search"><Search size={14}/>
      <input placeholder="搜索本篇笔记" aria-label="搜索本篇笔记" value={query} onChange={event => setQuery(event.target.value)}/>
    </div>}
    {filtered.length ? <AnnotationList annotations={filtered} selectedId={selectedId} pages={paper.pages} onChange={onChange}
      onOpenAnnotation={onOpenAnnotation} onAlign={onAlign} onAIAlign={onAIAlign}/> : <Empty icon={<Highlighter size={26}/>}
      title={keyword ? '没有找到匹配的笔记' : '把阅读中的想法留下'}
      text={keyword ? '试试其他关键词。' : '选中 PDF、原文或译文中的文字，即可高亮并添加笔记。'}/>}
  </div>;
}

export function AIPanel({paper,results,jobs,provider,onProvider,onQuestion,onSummary,onJump,onSaved,question,setQuestion}:{paper:Paper;results:AIResult[];jobs:Job[];provider:Provider;onProvider:(p:Provider)=>void;onQuestion:()=>void;onSummary:()=>void;onJump:(page:number,block?:string)=>void;onSaved:()=>void;question:string;setQuestion:(q:string)=>void}){
  const [error,setError]=useState(''),[saving,setSaving]=useState<string|null>(null);
  const working=jobs.find(j=>j.paper_id===paper.id&&j.kind==='reading'&&['queued','running'].includes(j.state));
  async function save(r:AIResult){try{await post('/ai-results/'+r.id+'/save-note');setSaving(r.id);onSaved();window.setTimeout(()=>setSaving(null),2000)}catch(e){setError((e as Error).message)}}
  const dialogue=results.filter(r=>r.kind==='question').slice().reverse(), records=results.filter(r=>r.kind!=='question');
  const thread=useRef<HTMLDivElement>(null), last=dialogue.at(-1);
  const pending=working?.payload.task==='question'&&typeof working.payload.question==='string'?working.payload.question:'';
  const failure=jobs.find(j=>j.paper_id===paper.id&&j.kind==='reading'&&j.payload.task==='question'&&j.state==='failed'&&j.updated>(last?.created??0));
  useEffect(()=>{if(thread.current)thread.current.scrollTop=thread.current.scrollHeight},[paper.id,last?.id,pending]);
  function answer(r:AIResult) {
    return <><p className="ai-answer">{r.answer}</p>
      {r.citations.length>0?<div className="citation-list">{r.citations.map((c,i)=><button key={i} title={c.quote} onClick={()=>onJump(c.page,c.block_id)}>第 {c.page} 页<ArrowUpRight size={11}/></button>)}</div>
        :<p className="small muted">暂无可跳转的原文出处。</p>}
      <div className="ai-result-footer"><small>{r.model} · {date(r.created)}</small><button className="text-button" onClick={()=>void save(r)}>{saving===r.id?<><Check size={12}/>已保存</>:<>存为笔记</>}</button></div></>;
  }
  return <div className="ai-panel paper-chat">
    <div className="ai-provider"><Sparkles size={15}/><select aria-label="AI 阅读模型" value={provider} onChange={e=>onProvider(e.target.value as Provider)}>{PROVIDERS.map(p=><option key={p} value={p}>{PROVIDER_NAMES[p]}</option>)}</select></div>
    <div className="paper-chat-intro"><h3>本篇论文助手</h3><p>只协助阅读当前论文，对话保存在本机。可以接着上次的问题追问。</p></div>
    <div className="paper-chat-thread" ref={thread} aria-label="本篇对话">
      {!dialogue.length&&!pending&&<p className="paper-chat-empty">可以从研究问题、方法，或一段不太好懂的原文问起。</p>}
      {dialogue.map(r=><div className="paper-chat-turn" key={r.id}><p className="paper-chat-question">{r.question}</p><article className="paper-chat-answer">{answer(r)}</article></div>)}
      {pending&&<p className="paper-chat-question">{pending}</p>}
      {working&&<div className="ai-working"><LoaderCircle size={16} className="spin"/><span>{working.message||'正在准备阅读任务…'}</span></div>}
    </div>
    <div className="question-box"><textarea aria-label="向文献提问" placeholder="向这篇论文提问或继续追问…" rows={3} value={question} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();if(question.trim()&&!working)onQuestion()}}}/><div><small>Ctrl + Enter 发送</small><button className="send-button" aria-label="发送问题" disabled={!question.trim()||!!working} onClick={onQuestion}><Send size={16}/></button></div></div>
    {provider!=='local'&&<p className="small muted paper-chat-provider-note">使用 {PROVIDER_NAMES[provider]} 时，会发送本篇相关段落及用于追问的对话上下文。</p>}
    <ErrorLine text={error||(!working?failure?.message??'':'')}/>
    <details className="paper-chat-records" open={!!working&&working.payload.task!=='question'||!!results[0]&&results[0].kind!=='question'}>
      <summary>摘要与选段分析{records.length>0?' · '+records.length:''}</summary>
      <button className="button compact" disabled={!!working} onClick={onSummary}><FileText size={15}/>生成结构化摘要</button>
      {records.map(r=><article className="ai-result" key={r.id}><div className="ai-result-meta"><span>{r.kind==='summary'?'摘要':r.kind==='explain'?'选段解释':'选段翻译'}</span><span>{r.scope}</span></div>{r.question&&<p className="ai-question">{r.question}</p>}{answer(r)}</article>)}
    </details>
  </div>
}

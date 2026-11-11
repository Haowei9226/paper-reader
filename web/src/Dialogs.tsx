import {useEffect,useRef,useState} from 'react';
import {Check,Download,FolderPlus,HardDrive,KeyRound,LoaderCircle,Power,RefreshCw,ShieldCheck,Sparkles,Upload} from 'lucide-react';
import {api,date,filesize,patch,post,remove} from './api';
import {Busy,ErrorLine,Modal} from './components';
import {relativeRange} from './selection';
import type {Annotation,Block,Bootstrap,CloudProvider,CloudStatus,Collection,Job,PageData,Paper,Selection,Settings,Translation} from './types';
import {ITEM_TYPE_LABELS,PUBLICATION_STATUS_LABELS,PROVIDERS,PROVIDER_NAMES} from './types';

export function CollectionDialog({collections,parent,move,onClose,onDone}: {
  collections:Collection[]; parent?:string; move?:Collection; onClose:()=>void; onDone:(parent:string)=>void;
}) {
  const [name,setName]=useState(''),[parentId,setParentId]=useState(move?.parent_id ?? parent ?? '');
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const excluded = new Set<string>();
  if (move) {
    const pending = [move.id];
    for (const id of pending) {
      if (excluded.has(id)) continue;
      excluded.add(id);
      for (const collection of collections) if (collection.parent_id === id) pending.push(collection.id);
    }
  }
  const canSave = !busy && (move ? parentId !== (move.parent_id ?? '') : !!name.trim());
  const close = () => {if (!busy) onClose();};
  async function save() {
    if (!canSave) return;
    setBusy(true);
    setError('');
    try {
      if (move) await patch('/collections/'+move.id,{parent_id:parentId});
      else await post('/collections',{name,parent_id:parentId});
      onDone(parentId);
      onClose();
    } catch (e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <Modal title={move?'移动集合':'新建集合'} onClose={close} footer={<>
    <button className="button" disabled={busy} onClick={close}>取消</button>
    <button className="button primary" disabled={!canSave} onClick={save}>{move?'移动集合':'创建集合'}</button>
  </>}>
    {move ? <p className="dialog-copy">将「{move.name}」及其子集合移动到：</p> :
      <label className="field">集合名称<input value={name} onChange={e=>setName(e.target.value)}
        placeholder="例如：博士研究 / 研究方法" onKeyDown={e=>{if(e.key==='Enter')void save()}}/></label>}
    <label className="field">放入<select value={parentId} onChange={e=>setParentId(e.target.value)}>
      <option value="">我的文献库</option>
      {collections.filter(c=>!excluded.has(c.id)).map(c=><option value={c.id} key={c.id}>{c.name}</option>)}
    </select></label>
    <ErrorLine text={error}/>
  </Modal>;
}

export type TrashAction = {action:'purge'|'restore'; paper?:Paper};

export function TrashDialog({action,paper,onClose,onDone}: TrashAction & {
  onClose:()=>void; onDone:(count:number,pendingCleanup:number)=>void;
}) {
  const [target,setTarget]=useState<{count:number;snapshot:string}>();
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const query = paper ? '?paper_id='+encodeURIComponent(paper.id) : '';
  const purge = action === 'purge';
  const title = purge ? paper ? '永久删除文献' : '清空回收站' : '恢复全部文献';
  useEffect(() => {
    const controller = new AbortController();
    void api<{count:number;snapshot:string}>('/trash'+query,{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted)setTarget(value);})
      .catch(e=>{if(!controller.signal.aborted)setError((e as Error).message);});
    return ()=>controller.abort();
  }, [query]);
  const close = () => {if (!busy) onClose();};
  async function apply() {
    if (!target?.count || busy) return;
    setBusy(true);
    setError('');
    try {
      const result=await post<{count:number;pending_cleanup:number}>(`/trash/${action}${query}`,{snapshot:target.snapshot});
      onDone(result.count,result.pending_cleanup);
      onClose();
    } catch (e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <Modal title={title} onClose={close} footer={<>
    <button className="button" onClick={close} disabled={busy}>取消</button>
    <button className={'button '+(purge?'danger':'primary')} disabled={!target?.count||busy} onClick={apply}>
      {busy?'正在处理…':purge?'永久删除':'恢复全部'}
    </button>
  </>}>
    {target ? target.count > 0 ? <>
      <p className="dialog-copy">{paper ? `将永久删除「${paper.title}」。` : purge ?
        `将永久删除回收站里的全部 ${target.count} 篇文献。` : `将恢复回收站里的全部 ${target.count} 篇文献。`}</p>
      {purge && <p className="dialog-copy muted">库内的 PDF、译文和笔记将一并删除。此操作无法撤销。</p>}
    </> : <p className="dialog-copy muted">没有可处理的回收站文献。</p> : !error && <Busy text="正在核对回收站"/>}
    <ErrorLine text={error}/>
  </Modal>;
}

export function TagDialog({tag,onClose,onDone}:{tag:string;onClose:()=>void;onDone:(newTag:string)=>void}) {
  const [name,setName]=useState(tag),[removing,setRemoving]=useState(false);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const close = () => {if (!busy) onClose();};
  async function save() {
    const value = name.trim();
    if (busy || (!removing && (!value || value===tag))) return;
    setBusy(true);
    setError('');
    try {
      const path = '/tags?name='+encodeURIComponent(tag);
      if (removing) await remove(path);
      else await patch(path,{name:value});
      onDone(removing?'':value);
      onClose();
    } catch (e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <Modal title={removing?'移除标签':'管理标签'} onClose={close} footer={<>
    {!removing && <button className="button danger" disabled={busy} onClick={()=>setRemoving(true)}>移除标签</button>}
    <button className="button" disabled={busy} onClick={close}>取消</button>
    <button className={'button '+(removing?'danger':'primary')} onClick={save}
      disabled={busy||(!removing&&(!name.trim()||name.trim()===tag))}>{removing?'确认移除':'保存名称'}</button>
  </>}>
    {removing ? <p className="dialog-copy">将从所有使用「{tag}」的文献中移除此标签。</p> : <>
      <label className="field">标签名称<input value={name} maxLength={80} onChange={e=>setName(e.target.value)}/></label>
      <p className="dialog-copy muted">输入已有标签的名称，可以把这两个标签合并。</p>
    </>}
    <ErrorLine text={error}/>
  </Modal>;
}

export function PaperDialog({paper,collections,onClose,onDone}:{paper:Paper;collections:Collection[];onClose:()=>void;onDone:()=>void}){
  const [value,setValue]=useState({...paper,tagsText:paper.tags.join(', ')}),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const initial=useRef(value);
  const change=(key:string,v:unknown)=>setValue(old=>({...old,[key]:v}));
  async function save(){
    setBusy(true);
    try{
      const fields=['title','authors','year','doi','abstract','source','item_type','publication_status','publication_date','publisher','language','filename'] as const;
      const updates:Record<string,unknown>=Object.fromEntries(fields.filter(key=>value[key]!==initial.current[key]).map(key=>[key,value[key]]));
      if(value.tagsText!==initial.current.tagsText)updates.tags=value.tagsText.split(/[,，;；]/).map(t=>t.trim()).filter(Boolean);
      if(JSON.stringify(value.collections)!==JSON.stringify(initial.current.collections))updates.collections=value.collections;
      await patch('/papers/'+paper.id,updates);onDone();onClose();
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <Modal title="编辑文献信息" wide onClose={onClose} footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={busy||!value.title.trim()||!value.filename.trim()} onClick={save}>保存修改</button></>}><label className="field">标题<input value={value.title} onChange={e=>change('title',e.target.value)}/></label><label className="field">作者<input value={value.authors} onChange={e=>change('authors',e.target.value)} placeholder="多位作者可用分号分隔"/></label><div className="form-grid"><label className="field">年份<input value={value.year} onChange={e=>change('year',e.target.value)} placeholder="如 2026"/></label><label className="field">原文语言<select value={value.language} onChange={e=>change('language',e.target.value)}><option value="en">英文</option><option value="zh">中文</option></select></label></div>
    <label className="field">发表状态<select value={value.publication_status??''} onChange={e=>change('publication_status',e.target.value)}>
      {Object.entries(PUBLICATION_STATUS_LABELS).map(([key,label])=><option key={key} value={key}>{label}</option>)}
    </select></label>
    {['unpublished','internal'].includes(value.publication_status)&&<p className="field-hint">适用于未发表稿或非发表用途的材料。自动书目补全会跳过此文献。</p>}
    <div className="form-grid">
      <label className="field">条目类型<select value={value.item_type} onChange={e=>change('item_type',e.target.value)}>
        <option value="">未填写</option>
        {Object.entries(ITEM_TYPE_LABELS).map(([key,label])=><option key={key} value={key}>{label}</option>)}
      </select></label>
      <label className="field">出版日期<input value={value.publication_date} onChange={e=>change('publication_date',e.target.value)}
        placeholder="如 2026-04-13 或 2026-04"/></label>
    </div>
    <label className="field">出版社<input value={value.publisher} onChange={e=>change('publisher',e.target.value)}/></label>
    <label className="field">期刊 / 会议 / 出版来源<input value={value.source} onChange={e=>change('source',e.target.value)}/></label><label className="field">DOI<input value={value.doi} onChange={e=>change('doi',e.target.value)} placeholder="10.xxxx/xxxx"/></label><label className="field">附件文件名<input value={value.filename} onChange={e=>change('filename',e.target.value)}/></label>{paper.original_filename&&<p className="small muted original-name">原导入名：{paper.original_filename}{value.filename!==paper.original_filename&&<button className="text-button" onClick={()=>change('filename',paper.original_filename)}>恢复原名</button>}</p>}<label className="field">摘要<textarea rows={4} value={value.abstract} onChange={e=>change('abstract',e.target.value)}/></label><label className="field">标签<input value={value.tagsText} onChange={e=>change('tagsText',e.target.value)} placeholder="用逗号分隔，例如：HCI, 协作, 研究方法"/></label>{collections.length>0&&<div className="field"><span>所属集合</span><div className="check-list">{collections.map(c=><label key={c.id}><input type="checkbox" checked={value.collections.includes(c.id)} onChange={e=>change('collections',e.target.checked?[...value.collections,c.id]:value.collections.filter(x=>x!==c.id))}/>{c.name}</label>)}</div></div>}<ErrorLine text={error}/></Modal>
}

export function NoteDialog({selection,onClose,onSave}:{selection:Selection|null;onClose:()=>void;onSave:(note:string,color:string)=>Promise<void>}){
  const [text,setText]=useState(''),[color,setColor]=useState('yellow'),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function save(){setBusy(true);try{await onSave(text,color);onClose()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <Modal title={selection?'为选中文字添加笔记':'新建独立笔记'} onClose={onClose} footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={busy||(!selection&&!text.trim())} onClick={save}>保存笔记</button></>}>
    {selection&&<blockquote className="quote-preview">{selection.text}</blockquote>}<label className="field">笔记<textarea autoFocus rows={6} value={text} onChange={e=>setText(e.target.value)} placeholder="记录你的想法、疑问或与研究的联系…"/></label><div className="color-options">{['yellow','green','blue','pink'].map((c,i)=><button key={c} aria-label={['黄色','绿色','蓝色','粉色'][i]} aria-pressed={color===c} className={`color-dot ${c} ${c===color?'chosen':''}`} onClick={()=>setColor(c)}/>)}</div><ErrorLine text={error}/>
  </Modal>
}

export function TranslationDialog({block,onClose,onDone}:{block:Block;onClose:()=>void;onDone:()=>void}){
  const [versions,setVersions]=useState<Translation[]>([]),[version,setVersion]=useState(block.translation?.id||''),[text,setText]=useState(block.translation?.text||''),[error,setError]=useState(''),[busy,setBusy]=useState(false);const dirty=useRef(false);
  useEffect(()=>{void api<Translation[]>('/blocks/'+block.id+'/translations').then(items=>{setVersions(items);const current=items.find(t=>t.active)||items[0];if(current&&!dirty.current){setVersion(current.id);setText(current.text)}}).catch(e=>setError(e.message))},[block.id]);
  async function save(){setBusy(true);try{await patch('/translations/'+version,{text});onDone();onClose()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <Modal title="译文与历史版本" onClose={onClose} wide footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" onClick={save} disabled={!text.trim()||busy||!version}>保存为新版本</button></>}><label className="field">查看版本<select value={version} onChange={e=>{dirty.current=true;setVersion(e.target.value);setText(versions.find(t=>t.id===e.target.value)?.text||'')}}>{versions.map(t=><option key={t.id} value={t.id}>{date(t.created)} · {t.manual?'人工修改':t.model}{t.active?' · 当前使用':''}</option>)}</select></label><div className="quote-preview">{block.text}</div><label className="field">译文<textarea rows={10} value={text} onChange={e=>{dirty.current=true;setText(e.target.value)}}/></label><p className="small muted">旧版本、笔记和摘录会保留。受修改影响的标注会进入待校对，可重新选择对应文字。</p><ErrorLine text={error}/></Modal>
}

export function AlignmentDialog({annotation,onClose,onDone}:{annotation:Annotation;onClose:()=>void;onDone:()=>void}){
  const [block,setBlock]=useState<Block|null>(null),[translation,setTranslation]=useState<Translation|null>(null),[sourceRange,setSourceRange]=useState<{start:number;end:number}|null>(annotation.targets[0]||null),[targetRange,setTargetRange]=useState<{start:number;end:number}|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const source=useRef<HTMLParagraphElement>(null),target=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{const t=annotation.targets[0];if(!t){setError('独立笔记无需修正文字对应');return}void api<PageData>(`/papers/${annotation.paper_id}/pages/${t.page||1}`).then(data=>{const b=data.blocks.find(b=>b.id===t.block_id);if(!b)throw new Error('原文段落不存在');setBlock(b);setTranslation(b.translation);const old=annotation.translated_targets.find(x=>x.translation_id===b.translation?.id);if(old)setTargetRange(old)}).catch(e=>setError(e.message))},[annotation.id]);
  async function save(){if(!block||!sourceRange||!translation||!targetRange)return;setBusy(true);try{const otherSource=annotation.targets.filter(t=>t.block_id!==block.id);const otherTarget=annotation.translated_targets.filter(t=>t.block_id!==block.id);await patch('/annotations/'+annotation.id,{targets:[...otherSource,{block_id:block.id,...sourceRange}],translated_targets:[...otherTarget,{block_id:block.id,translation_id:translation.id,...targetRange}]});onDone();onClose()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const paint=(text:string,range:{start:number;end:number}|null)=>range?<>{text.slice(0,range.start)}<mark className="hl-blue">{text.slice(range.start,range.end)}</mark>{text.slice(range.end)}</>:text;
  return <Modal title="人工修正对应文字" wide onClose={onClose} footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={!sourceRange||!targetRange||busy} onClick={save}>保存对应关系</button></>}><p className="muted">分别在下面两段文字中选中对应内容。蓝色区域是将要保存的范围。</p>{!block&&!error?<Busy/>:block&&<><div className="alignment-columns"><div><h3>原文 · 第 {block.page} 页</h3><p className="alignment-text" ref={source} onMouseUp={()=>{if(source.current){const r=relativeRange(source.current);if(r)setSourceRange(r)}}}>{paint(block.text,sourceRange)}</p></div><div><h3>当前译文</h3>{translation?<p className="alignment-text" ref={target} onMouseUp={()=>{if(target.current){const r=relativeRange(target.current);if(r)setTargetRange(r)}}}>{paint(translation.text,targetRange)}</p>:<p className="muted">请先翻译此段，再修正双语对应。</p>}</div></div>{annotation.translated_targets.length>0&&<details className="old-excerpt"><summary>查看笔记中保留的译文摘录</summary>{annotation.translated_targets.map((t,i)=><p key={i}>{t.quote}</p>)}</details>}</>}<ErrorLine text={error}/></Modal>
}

type OcrStatus = {
  available: boolean;
  languages: {tag:string;name:string}[];
  default_language: string | null;
  reason?: string;
};

export function OcrDialog({paper, pages, onClose, onStarted}: {
  paper: Paper;
  pages: number;
  onClose: () => void;
  onStarted: () => void;
}) {
  const [status,setStatus] = useState<OcrStatus|null>(null);
  const [language,setLanguage] = useState('');
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');

  useEffect(() => {
    const request = new AbortController();
    void api<OcrStatus>('/ocr/status',{signal:request.signal}).then(value => {
      if (request.signal.aborted) return;
      setStatus(value);
      const preferred = value.languages.find(item => item.tag.split('-')[0] === paper.language);
      setLanguage(preferred?.tag ?? value.default_language ?? value.languages[0]?.tag ?? '');
    }).catch((error:Error) => {if (!request.signal.aborted) setError(error.message)});
    return () => request.abort();
  }, [paper.language]);

  async function start() {
    setBusy(true);
    setError('');
    try {
      await post(`/papers/${paper.id}/jobs`,{kind:'ocr',language});
      onStarted();
      onClose();
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <Modal title="本地 OCR" onClose={onClose} footer={<>
    <button className="button" onClick={onClose}>关闭</button>
    <button className="button primary" disabled={!status?.available || !language || busy}
      onClick={() => void start()}>{busy?'正在加入队列…':'开始识别'}</button>
  </>}>
    <p>将在本机识别 {pages} 个无可提取文字的页面。识别后的文字可用于查找、翻译和批注。</p>
    {status?.available && <label className="field">识别语言
      <select value={language} onChange={event => setLanguage(event.target.value)}>
        {status.languages.map(item => <option key={item.tag} value={item.tag}>{item.name}</option>)}
      </select>
    </label>}
    {!status && !error && <Busy text="正在检查本机的识别语言"/>}
    {status && !status.available && <p className="small muted">{status.reason || '请在 Windows 语言设置中安装所需语言的光学字符识别功能。'}</p>}
    <ErrorLine text={error}/>
  </Modal>;
}

export function JobsDialog({jobs,onClose,onChange}:{jobs:Job[];onClose:()=>void;onChange:()=>void}){
  const [error,setError]=useState(''),[busy,setBusy]=useState<string|null>(null);
  async function action(job:Job,name:string){
    setError('');setBusy(job.id);
    try{await post(`/jobs/${job.id}/${name}`);onChange()}catch(e){setError((e as Error).message)}finally{setBusy(null)}
  }
  const labels:Record<string,string>={parse:'PDF 转 HTML',ocr:'本地 OCR',metadata:'文献信息补全',translate:'双语翻译',reading:'AI 阅读',align:'短语对应',backup:'文献库备份'};
  const states:Record<string,string>={queued:'等待中',running:'正在处理',paused:'已暂停',done:'已完成',partial:'部分完成',failed:'需要处理',cancelled:'已取消'};
  return <Modal title="任务与进度" wide onClose={onClose}>
    <p className="muted small">已完成的译文会保留。继续翻译时，只处理还没完成的段落。</p><ErrorLine text={error}/>
    {!jobs.length?<p className="muted space-y">还没有任务。导入文献后，会在这里显示转换进度。</p>:jobs.map(j=>{
      const rebuild=j.kind==='parse'&&j.payload.rebuild===true;
      const fillPublication=j.kind==='metadata'&&j.payload.fill_publication_fields===true;
      const preservedPages=rebuild&&Array.isArray(j.result.preserved_pages)
        ? j.result.preserved_pages.filter((page):page is number=>typeof page==='number') : [];
      const start=j.payload.page_start,end=j.payload.page_end;
      const scope=j.kind!=='translate'?'':typeof start==='number'
        ? `第 ${start}${typeof end==='number'&&end!==start?'–'+end:''} 页`
        : Array.isArray(j.payload.block_ids)&&j.payload.block_ids.length?`所选 ${j.payload.block_ids.length} 段`:'全文';
      const failedPages=j.kind==='ocr'&&Array.isArray(j.result.failed_pages)
        ? j.result.failed_pages.filter((page):page is number=>typeof page==='number')
        : Array.isArray(j.result.failed_blocks)?[...new Set(j.result.failed_blocks.flatMap(b=>typeof b?.page==='number'?[b.page]:[]))].sort((a,b)=>a-b):[];
      return <div className="job-item" key={j.id}>
        <div className="job-title"><strong>{rebuild?'重新转换 HTML':fillPublication?'补全出版信息':labels[j.kind]||j.kind}</strong><span className={'job-state '+j.state}>{states[j.state]||j.state}</span></div>
        <p className="job-paper">{j.paper_title||'我的文献库'}{scope&&` · ${scope}`}</p>
        {j.total>0&&<progress max={j.total} value={j.progress} aria-label={j.kind==='translate'?'已完成段落':'任务进度'}/>}
        <p className={j.state==='failed'?'error-line small':'muted small'}>{j.message||'等待开始'}{j.total>0?` · ${j.progress}/${j.total}` : ''}</p>
        {!!preservedPages.length&&<p className="small muted job-missing-pages">沿用原阅读版的页码：{preservedPages.slice(0,10).join('、')}{preservedPages.length>10?'等':''}</p>}
        {!!failedPages.length&&['partial','failed'].includes(j.state)&&<p className="small muted job-missing-pages">{j.kind==='ocr'?'待识别':'待补译'}第 {failedPages.slice(0,10).join('、')} 页{failedPages.length>10?'等':''}</p>}
        <div className="job-actions">
          {['queued','running'].includes(j.state)&&<button className="text-button" disabled={busy===j.id} onClick={()=>action(j,'pause')}>暂停</button>}
          {['paused','failed','partial'].includes(j.state)&&<button className="text-button" disabled={busy===j.id} onClick={()=>action(j,'resume')}>{rebuild?'重新开始':j.state==='partial'&&j.kind==='translate'?'补译剩余':j.kind==='translate'?'继续翻译':j.state==='failed'?'重试':'继续'}</button>}
          {!['done','cancelled'].includes(j.state)&&<button className="text-button muted" disabled={busy===j.id} onClick={()=>action(j,'cancel')}>取消任务</button>}
          {j.kind==='backup'&&j.state==='done'&&typeof j.result.filename==='string'&&<a className="text-button" href={'/api/backups/'+j.result.filename}>下载备份</a>}
        </div>
      </div>;
    })}
  </Modal>;
}

export function SettingsDialog({bootstrap,jobs,onClose,onDone,onRestored,onJobs,onQuit}:{bootstrap:Bootstrap;jobs:Job[];onClose:()=>void;onDone:()=>void;onRestored:()=>void;onJobs:()=>void;onQuit:()=>void}){
  const [tab,setTab]=useState<'models'|'import'|'backup'>('models'),[settings,setSettings]=useState<Settings>(bootstrap.settings);
  const [keys,setKeys]=useState<Record<CloudProvider,string>>({deepseek:'',openai:''});
  const [configured,setConfigured]=useState<Record<CloudProvider,boolean>>({deepseek:bootstrap.deepseek_configured,openai:bootstrap.openai_configured});
  const [error,setError]=useState(''),[success,setSuccess]=useState(''),[busy,setBusy]=useState(false),[backups,setBackups]=useState<{filename:string;size:number;created:number}[]>([]),[restoreFile,setRestoreFile]=useState<File|null>(null);
  const restoreInput=useRef<HTMLInputElement>(null);
  const change=<K extends keyof Settings>(k:K,v:Settings[K])=>setSettings(s=>({...s,[k]:v}));
  const cloud=settings.provider==='local'?null:settings.provider;
  const reloadBackups=()=>api<typeof backups>('/backups').then(setBackups).catch(e=>setError(e.message));
  useEffect(()=>{if(tab==='backup')void reloadBackups()},[tab,jobs.filter(j=>j.kind==='backup'&&j.state==='done').length]);
  async function save(close=false){
    setError('');setSuccess('');setBusy(true);
    try{
      const value=await patch<Settings&CloudStatus>('/settings',{...settings,deepseek_api_key:keys.deepseek||undefined,openai_api_key:keys.openai||undefined});
      setConfigured({deepseek:value.deepseek_configured,openai:value.openai_configured});
      setKeys({deepseek:'',openai:''});onDone();setSuccess('设置已保存');
      if(close)onClose();
      return true;
    }catch(e){setError((e as Error).message);return false}
    finally{setBusy(false)}
  }
  async function test(){
    if(!await save())return;
    setBusy(true);setSuccess('正在测试连接…');
    try{
      const result=await post<{model:string}>('/models/test',{provider:settings.provider});
      setSuccess('连接成功 · '+result.model);
    }catch(e){setSuccess('');setError((e as Error).message)}
    finally{setBusy(false)}
  }
  async function quit(){
    setError('');setSuccess('');setBusy(true);
    try{await post('/app/quit');onQuit()}
    catch(e){setError((e as Error).message);setBusy(false)}
  }
  async function backup(){try{await post('/backups');onJobs();setSuccess('备份任务已加入队列，可在任务面板查看进度')}catch(e){setError((e as Error).message)}}
  async function restore(){if(!restoreFile)return;setBusy(true);setError('');const form=new FormData();form.append('file',restoreFile);try{const result=await api<{previous_copy:string}>('/restore',{method:'POST',body:form});setSuccess('文献库已恢复，之前的数据保留在 '+result.previous_copy);setRestoreFile(null);onRestored();void reloadBackups()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <Modal title="设置" wide onClose={()=>{if(!busy)onClose()}} footer={<>
    <button className="button quit-reader" disabled={busy} onClick={quit}><Power size={15}/>退出阅读器</button>
    {tab==='models'?<><button className="button" disabled={busy} onClick={test}><ShieldCheck size={15}/>测试当前模型</button><button className="button primary" disabled={busy} onClick={()=>save(true)}>保存设置</button></>:tab==='import'?<button className="button primary" disabled={busy} onClick={()=>save(true)}>保存设置</button>:<button className="button" disabled={busy} onClick={onClose}>完成</button>}
  </>}>
    <div className="settings-tabs"><button className={tab==='models'?'selected':''} onClick={()=>setTab('models')}>模型与翻译</button><button className={tab==='import'?'selected':''} onClick={()=>setTab('import')}>导入与命名</button><button className={tab==='backup'?'selected':''} onClick={()=>setTab('backup')}>备份与迁移</button></div>
    {tab==='models'?<>
      <div className="provider-options">{PROVIDERS.map(provider=>{
        const Icon=provider==='local'?HardDrive:provider==='deepseek'?KeyRound:Sparkles;
        return <button key={provider} disabled={busy} className={'provider-option '+(settings.provider===provider?'selected':'')} aria-pressed={settings.provider===provider} onClick={()=>{change('provider',provider);setError('');setSuccess('')}}>
          <Icon size={20}/><strong>{PROVIDER_NAMES[provider]}{provider==='local'?'':' API'}</strong><span>{provider==='local'?'离线处理':configured[provider]?'密钥已配置':'待配置密钥'}</span>
        </button>
      })}</div>
      {cloud?<div className="settings-section">
        <h3>{PROVIDER_NAMES[cloud]} API</h3>
        <label className="field">API Key <span className="field-hint">{configured[cloud]?'已配置，留空保留现有密钥':'尚未配置'}</span>
          <input type="password" aria-label={PROVIDER_NAMES[cloud]+' API Key'} autoComplete="off" value={keys[cloud]} onChange={e=>setKeys({...keys,[cloud]:e.target.value})} placeholder={configured[cloud]?'已配置':'粘贴 API Key'}/>
        </label>
        <label className="field">模型名称<input value={settings[cloud==='openai'?'openai_model':'deepseek_model']} onChange={e=>change(cloud==='openai'?'openai_model':'deepseek_model',e.target.value)}/></label>
        <p className="small muted">在此输入的密钥由 Windows 加密或 macOS 钥匙串保存在本机。使用 {PROVIDER_NAMES[cloud]} 执行任务时，所选文献内容会发送至该服务。</p>
      </div>:<div className="settings-section"><h3>本地模型连接</h3>
        <div className="form-grid"><label className="field">模型名称<input value={settings.local_model} onChange={e=>change('local_model',e.target.value)}/></label><label className="field">上下文长度<select value={settings.context_tokens} onChange={e=>change('context_tokens',Number(e.target.value))}><option value={4096}>4096 tokens</option><option value={8192}>8192 tokens</option><option value={16384}>16384 tokens</option></select></label></div>
        <label className="field">Ollama 地址<input value={settings.local_url} onChange={e=>change('local_url',e.target.value)}/></label>
        <p className="small muted">请先在本机安装并运行 Ollama，再填写服务地址和已下载的模型名称。较大的上下文会增加内存需求。</p>
      </div>}
    </>:tab==='import'?<div className="import-settings">
      <label className="import-option"><input type="checkbox" checked={!!settings.metadata_lookup} onChange={e=>change('metadata_lookup',e.target.checked)}/><span><strong>导入后自动联网补全文献信息</strong><small>使用 DOI、arXiv 编号或标题查公开书目。PDF 和笔记保存在本机。</small></span></label>
      <label className="import-option"><input type="checkbox" checked={!!settings.auto_rename} onChange={e=>change('auto_rename',e.target.checked)}/><span><strong>根据文献信息命名附件</strong><small>使用首位作者、年份和标题，例如：作者 - 2024 - 论文标题.pdf。</small></span></label>
    </div>:<><div className="backup-intro"><HardDrive size={25}/><div><h3>把整个文献库带走</h3><p>包括 PDF、文献信息、译文、笔记及人工对应关系。API 密钥单独保留在本机。</p></div></div><button className="button primary" onClick={backup} disabled={busy}><Download size={16}/>创建完整备份</button><div className="backup-list">{backups.map(b=><a href={'/api/backups/'+b.filename} key={b.filename}><span><Download size={15}/>{b.filename}</span><small>{filesize(b.size)}</small></a>)}{!backups.length&&<p className="muted small">备份完成后会出现在这里。</p>}</div><div className="settings-section"><h3>恢复文献库</h3><p className="muted small">恢复会替换当前文献库。程序会保留当前数据的完整副本，便于找回。</p><input hidden type="file" accept=".zip" ref={restoreInput} onChange={e=>setRestoreFile(e.target.files?.[0]||null)}/><button className="button" disabled={busy} onClick={()=>restoreInput.current?.click()}><Upload size={15}/>选择备份文件</button>{restoreFile&&<div className="restore-confirm"><span>{restoreFile.name} · {filesize(restoreFile.size)}</span><button className="button danger" disabled={busy} onClick={restore}>{busy?'正在恢复…':'恢复这份备份'}</button></div>}</div><div className="metadata-export-options"><a className="text-button" href="/api/export/metadata">CSV</a><a className="text-button" href="/api/export/metadata?format=bibtex">BibTeX</a><a className="text-button" href="/api/export/metadata?format=ris">RIS</a></div></>}
  <ErrorLine text={error}/>{success&&<p className="success-line" role="status">{success}</p>}</Modal>
}

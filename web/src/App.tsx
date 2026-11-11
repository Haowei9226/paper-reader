import {useCallback,useEffect,useRef,useState} from 'react';
import {Archive,ArrowLeft,BookMarked,BookOpen,Check,ChevronDown,ChevronRight,Clock3,Columns3,FileText,Folder,FolderInput,FolderOpen,FolderPlus,Globe2,Highlighter,Inbox,Languages,Layers3,Library,LoaderCircle,MoreHorizontal,PanelRight,Plus,Search,Settings2,Star,Tag,Trash2,Undo2,Upload,X} from 'lucide-react';
import {api,patch,post,remove} from './api';
import {Busy,Empty,ErrorLine,IconButton,Modal} from './components';
import {CollectionDialog,JobsDialog,PaperDialog,SettingsDialog,TagDialog,TrashDialog,type TrashAction} from './Dialogs';
import BatchJobsDialog, {type BatchAction,type BatchResult} from './BatchJobsDialog';
import LibraryTable, {LibraryColumnsDialog,librarySortOptions,rememberLibraryColumns,rememberLibrarySort,restoreLibraryColumns,
  restoreLibrarySort,sortValue,type LibraryColumn,type LibrarySort} from './LibraryTable';
import NotesOverview from './NotesOverview';
import {InfoPanel} from './Panels';
import PaperNotes, {type NotePaperGroup} from './PaperNotes';
import ReaderScreen, {type ReferenceReturn} from './ReaderScreen';
import type {Annotation,Bootstrap,Collection,Job,Paper} from './types';
import {STATUS_LABELS} from './types';

const paperPageSize = 100;

export default function App(){
  const [bootstrap,setBootstrap]=useState<Bootstrap|null>(null),[papers,setPapers]=useState<Paper[]>([]),[total,setTotal]=useState(0),[selectedId,setSelectedId]=useState<string|null>(null),[selected,setSelected]=useState<Paper|null>(null),[readerId,setReaderId]=useState<string|null>(null),[readerPaper,setReaderPaper]=useState<Paper|null>(null),[filter,setFilter]=useState('all'),[query,setQuery]=useState(''),[effectiveQuery,setEffectiveQuery]=useState(''),[limit,setLimit]=useState(100),[revision,setRevision]=useState(0),[loading,setLoading]=useState(true),[fatal,setFatal]=useState(''),[toast,setToast]=useState(''),[uploading,setUploading]=useState(false),[dragging,setDragging]=useState(false),[jobs,setJobs]=useState<Job[]>([]),[showJobs,setShowJobs]=useState(false),[showSettings,setShowSettings]=useState(false),[collectionDialog,setCollectionDialog]=useState<string|false>(false),[editingPaper,setEditingPaper]=useState(false),[details,setDetails]=useState(window.innerWidth>1100),[moreTags,setMoreTags]=useState(false),[collapsed,setCollapsed]=useState<Set<string>>(new Set()),[contextCollection,setContextCollection]=useState<Collection|null>(null),[prompt,setPrompt]=useState<{title:string;value:string;save:(value:string)=>Promise<unknown>}|null>(null);
  const [noteGroups,setNoteGroups] = useState<NotePaperGroup[]>([]);
  const [notesError,setNotesError] = useState('');
  const [notesWorkspace,setNotesWorkspace] = useState<{paperId:string;query:string}|null>(null);
  const [notesPaper,setNotesPaper] = useState<Paper|null>(null);
  const [notesPaperError,setNotesPaperError] = useState('');
  const [readerAnnotation,setReaderAnnotation] = useState<Annotation|null>(null);
  const [referenceTrail,setReferenceTrail] = useState<ReferenceReturn[]>([]);
  const [referenceReturn,setReferenceReturn] = useState<ReferenceReturn>();
  const [navigatingReference,setNavigatingReference] = useState(false);
  const referenceRequest = useRef<AbortController|null>(null);
  const notesPaperId = notesWorkspace?.paperId;
  const noteGroupList = useRef<HTMLDivElement>(null);
  const [exited,setExited]=useState(false);
  const [sort, setSort] = useState<LibrarySort>(restoreLibrarySort);
  const [columns,setColumns] = useState<LibraryColumn[]>(restoreLibraryColumns);
  const [showColumns,setShowColumns] = useState(false);
  const [checked,setChecked] = useState<Set<string>>(()=>new Set());
  const [batchDialog,setBatchDialog] = useState<{action:BatchAction;papers:Paper[]}|null>(null);
  const [batchUpdating,setBatchUpdating] = useState(false);
  const [trashAction,setTrashAction]=useState<TrashAction|null>(null);
  const [movingCollection,setMovingCollection]=useState<Collection|null>(null);
  const [tagDialog,setTagDialog]=useState<string|null>(null);
  const libraryTable = useRef<HTMLDivElement>(null);
  const fileInput=useRef<HTMLInputElement>(null),searchInput=useRef<HTMLInputElement>(null),dragDepth=useRef(0),jobSignature=useRef(''),toastTimer=useRef<number|undefined>(undefined);
  const notify=useCallback((message:string)=>{setToast(message);window.clearTimeout(toastTimer.current);toastTimer.current=window.setTimeout(()=>setToast(''),6500)},[]);
  const refresh=useCallback(()=>setRevision(v=>v+1),[]);
  useEffect(()=>{const timer=window.setTimeout(()=>setEffectiveQuery(query.trim()),250);return()=>clearTimeout(timer)},[query]);
  useEffect(()=>{let active=true;void api<Bootstrap>('/bootstrap').then(b=>{if(active){setBootstrap(b);setFatal('')}}).catch(e=>{if(active)setFatal(e.message)});return()=>{active=false}},[revision]);
  useEffect(()=>{
    let active = true;
    setLoading(true);
    const params = new URLSearchParams({
      q: effectiveQuery, limit: String(paperPageSize), offset: String(limit - paperPageSize),
    });
    if (filter === 'notes') {
      setNotesError('');
      void api<{items:NotePaperGroup[];total:number}>('/annotation-papers?'+params).then(result=>{
        if (!active) return;
        const lastPage = Math.max(1, Math.ceil(result.total / paperPageSize)) * paperPageSize;
        if (limit > lastPage) {
          setLimit(lastPage);
          return;
        }
        setNoteGroups(result.items);
        setTotal(result.total);
        setLoading(false);
      }).catch((error:Error)=>{
        if (!active) return;
        setNotesError(error.message);
        setLoading(false);
      });
    } else {
      params.set('sort', sort.field);
      params.set('direction', sort.direction);
      if (filter.startsWith('c:')) params.set('collection',filter.slice(2));
      else if (filter.startsWith('t:')) params.set('tag',filter.slice(2));
      else if (filter==='starred') params.set('starred','true');
      else if (filter==='trash') params.set('trash','true');
      else if (['unread','reading','read'].includes(filter)) params.set('status',filter);
      void api<{items:Paper[];total:number}>('/papers?'+params).then(result=>{
        if (!active) return;
        const lastPage = Math.max(1, Math.ceil(result.total / paperPageSize)) * paperPageSize;
        if (limit > lastPage) {
          setLimit(lastPage);
          setChecked(new Set());
          return;
        }
        setPapers(result.items);
        setTotal(result.total);
        setSelectedId(old=>result.items.some(p=>p.id===old)?old:result.items[0]?.id||null);
        setChecked(old=>{
          if (!old.size) return old;
          const current = result.items.filter(paper=>old.has(paper.id)).map(paper=>paper.id);
          return current.length===old.size ? old : new Set(current);
        });
        setLoading(false);
      }).catch((error:Error)=>{
        if (!active) return;
        setFatal(error.message);
        setLoading(false);
      });
    }
    return ()=>{active=false};
  },[filter,effectiveQuery,sort,limit,revision]);
  useEffect(()=>{
    if (filter !== 'notes' || !notesPaperId) return;
    let active = true;
    setNotesPaperError('');
    void api<Paper>('/papers/'+notesPaperId).then(paper=>{
      if (!active) return;
      if (paper.deleted_at) {
        setNotesPaper(null);
        setNotesPaperError('这篇文献已移入回收站。');
        return;
      }
      setNotesPaper(paper);
    }).catch((error:Error)=>{
      if (!active) return;
      setNotesPaper(null);
      setNotesPaperError(error.message);
    });
    return ()=>{active=false};
  },[filter,notesPaperId,revision]);
  useEffect(()=>{if(!selectedId){setSelected(null);return}let active=true;void api<Paper>('/papers/'+selectedId).then(p=>{if(active)setSelected(p)}).catch(()=>{if(active)setSelected(null)});return()=>{active=false}},[selectedId,revision]);
  useEffect(()=>{if(!readerId){setReaderPaper(null);return}let active=true;void api<Paper>('/papers/'+readerId).then(p=>{if(active)setReaderPaper(p)}).catch(e=>notify(e.message));return()=>{active=false}},[readerId,revision,notify]);
  useEffect(()=>{if(exited)return;let active=true,timer:number;async function poll(){try{const value=await api<Job[]>('/jobs');if(!active)return;setJobs(value);const signature=value.map(j=>j.id+':'+j.state+':'+j.progress).join('|');if(signature!==jobSignature.current){jobSignature.current=signature;refresh()}}catch{}if(active)timer=window.setTimeout(poll,2000)}void poll();return()=>{active=false;clearTimeout(timer)}},[refresh,exited]);
  useEffect(()=>{const key=(e:KeyboardEvent)=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='f'&&!readerId){e.preventDefault();searchInput.current?.focus()}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='o'){e.preventDefault();fileInput.current?.click()}};document.addEventListener('keydown',key);return()=>document.removeEventListener('keydown',key)},[readerId]);
  async function importFiles(files:FileList|File[]){if(!files.length)return;const pdfs=Array.from(files).filter(f=>f.name.toLowerCase().endsWith('.pdf'));if(!pdfs.length){notify('请选择 PDF 文件');return}setUploading(true);const form=new FormData();pdfs.forEach(f=>form.append('files',f));try{const collection=filter.startsWith('c:')?'?collection='+filter.slice(2):'';const result=await api<{imported:Paper[];duplicates:{id:string;filename:string}[];errors:{filename:string;message:string}[]}>('/import'+collection,{method:'POST',body:form});refresh();const pieces=[];if(result.imported.length){pieces.push(`已导入 ${result.imported.length} 篇，正在转换 HTML`);setSelectedId(result.imported[0].id)}if(result.duplicates.length)pieces.push(`${result.duplicates.length} 份相同文件已在文献库中`);if(result.errors.length)pieces.push(result.errors.map(e=>e.filename+'：'+e.message).join('；'));notify(pieces.join('；'));if(filter==='trash'||filter==='notes')chooseFilter('all')}catch(e){notify((e as Error).message)}finally{setUploading(false);if(fileInput.current)fileInput.current.value=''}}
  function chooseFilter(value:string){
    setFilter(value);
    setSelectedId(null);
    setChecked(new Set());
    setLimit(paperPageSize);
    setContextCollection(null);
    setNotesWorkspace(null);
  }

  function changeSearch(value:string) {
    setQuery(value);
    setChecked(new Set());
    setLimit(paperPageSize);
    setNotesWorkspace(null);
    if (noteGroupList.current) noteGroupList.current.scrollTop = 0;
  }

  function finishTrash(count:number,pendingCleanup:number) {
    setLimit(paperPageSize);
    setSelectedId(null);
    setChecked(new Set());
    refresh();
    const action = trashAction?.action==='purge' ? '永久删除' : '恢复';
    notify(`已${action} ${count} 篇文献` + (pendingCleanup ? `，还有 ${pendingCleanup} 篇的附件将在重启后继续清理` : ''));
  }

  function finishTag(newTag:string) {
    if (filter === 't:'+tagDialog) chooseFilter(newTag ? 't:'+newTag : 'all');
    refresh();
    notify(newTag ? '标签已更新' : '标签已移除');
  }

  function changeSort(next: LibrarySort) {
    setSort(next);
    setChecked(new Set());
    setLimit(paperPageSize);
    if (libraryTable.current) libraryTable.current.scrollTop = 0;
    rememberLibrarySort(next);
  }

  function changePaperPage(next:number) {
    setChecked(new Set());
    setLimit(next);
    if (libraryTable.current) libraryTable.current.scrollTop = 0;
  }

  const batchPapers = papers.filter(paper=>checked.has(paper.id));
  const canBatch = !loading && query.trim()===effectiveQuery && !batchUpdating;
  async function markBatch(status:Paper['status']) {
    setBatchUpdating(true);
    try {
      const result = await post<BatchResult>('/papers/batch',{action:'status',status,paper_ids:batchPapers.map(paper=>paper.id)});
      setChecked(new Set());
      refresh();
      notify(result.skipped.length
        ? `已更新 ${result.updated} 篇，${result.skipped.length} 篇已不可用`
        : result.updated ? `已将 ${result.updated} 篇设为${STATUS_LABELS[status]}` : `所选文献均为${STATUS_LABELS[status]}`);
    } catch (error) {
      notify((error as Error).message);
    } finally {
      setBatchUpdating(false);
    }
  }

  function acceptQueuedBatch(result:BatchResult) {
    if (!result.queued.length) return;
    const queuedIds = new Set(result.queued.map(job=>job.id));
    setJobs(current=>[...result.queued.slice().reverse(),...current.filter(job=>!queuedIds.has(job.id))]);
    setChecked(new Set(result.skipped.map(item=>item.paper_id)));
    refresh();
  }

  function showReader(p:Paper, annotation:Annotation|null=null){
    setReaderAnnotation(annotation);
    setReaderPaper(p);
    setReaderId(p.id);
    if (!p.deleted_at) void patch('/papers/'+p.id,{opened:true,...(!annotation&&p.status==='unread'?{status:'reading'}:{})})
      .then(refresh).catch(()=>{});
  }
  function resetReferenceTrail() {
    referenceRequest.current?.abort();
    referenceRequest.current = null;
    setNavigatingReference(false);
    setReferenceReturn(undefined);
    setReferenceTrail([]);
  }
  function openReader(p:Paper, annotation:Annotation|null=null) {
    resetReferenceTrail();
    showReader(p,annotation);
  }
  const previousReference = referenceTrail.at(-1);
  async function navigateReference(paperId:string, from?:ReferenceReturn) {
    if (paperId === readerId || referenceRequest.current) return;
    const request = new AbortController();
    referenceRequest.current = request;
    setNavigatingReference(true);
    try {
      const target = await api<Paper>(`/papers/${paperId}`,{signal:request.signal});
      if (request.signal.aborted) return;
      if (target.deleted_at !== null) throw new Error('这篇文献已移入回收站');
      setReferenceReturn(from ? undefined : previousReference);
      setReferenceTrail(trail => from ? [...trail,from] : trail.slice(0,-1));
      showReader(target);
    } catch (error) {
      if (!request.signal.aborted) notify((error as Error).message);
    } finally {
      if (referenceRequest.current === request) {
        referenceRequest.current = null;
        setNavigatingReference(false);
      }
    }
  }
  function openPaperNotes(paper:Paper) {
    chooseFilter('notes');
    setQuery('');
    setEffectiveQuery('');
    setNotesPaper(paper);
    setNotesPaperError('');
    setNotesWorkspace({paperId:paper.id,query:''});
  }
  function closePaperNotes() {
    setNotesWorkspace(null);
    window.requestAnimationFrame(()=>{
      noteGroupList.current?.querySelector<HTMLButtonElement>(`[data-note-paper="${notesPaperId}"]`)?.focus({preventScroll:true});
    });
  }
  async function dropInto(e:React.DragEvent,c:Collection){e.preventDefault();e.stopPropagation();const id=e.dataTransfer.getData('application/x-reader-paper');if(!id)return;try{const p=await api<Paper>('/papers/'+id);await patch('/papers/'+id,{collections:Array.from(new Set([...p.collections,c.id]))});refresh();notify('已加入“'+c.name+'”')}catch(e){notify((e as Error).message)}}
  function collectionTree(parent:string|null=null,depth=0):React.ReactNode[]{if(!bootstrap||depth>15)return [];return bootstrap.collections.filter(c=>c.parent_id===parent).flatMap(c=>{const hasChildren=bootstrap.collections.some(x=>x.parent_id===c.id);const isCollapsed=collapsed.has(c.id);return [<div className="collection-row" key={c.id} onDragOver={e=>{if(e.dataTransfer.types.includes('application/x-reader-paper')){e.preventDefault();e.currentTarget.classList.add('drop-target')}}} onDragLeave={e=>e.currentTarget.classList.remove('drop-target')} onDrop={e=>{e.currentTarget.classList.remove('drop-target');void dropInto(e,c)}}><button className={'nav-item collection-nav '+(filter==='c:'+c.id?'selected':'')} style={{paddingLeft:14+depth*14}} onClick={()=>chooseFilter('c:'+c.id)} onContextMenu={e=>{e.preventDefault();setContextCollection(c)}}>{hasChildren?<span className="tree-chevron" onClick={e=>{e.stopPropagation();setCollapsed(old=>{const value=new Set(old);if(value.has(c.id))value.delete(c.id);else value.add(c.id);return value})}}>{isCollapsed?<ChevronRight size={12}/>:<ChevronDown size={12}/>}</span>:<span className="tree-placeholder"/>}{filter==='c:'+c.id?<FolderOpen size={16}/>:<Folder size={16}/>}<span>{c.name}</span>{c.count>0&&<small>{c.count}</small>}</button><button className="collection-more" aria-label={'管理集合 '+c.name} title="管理集合" onClick={()=>setContextCollection(contextCollection?.id===c.id?null:c)}><MoreHorizontal size={14}/></button></div>,...(!isCollapsed?collectionTree(c.id,depth+1):[])]})}
  const activeJobs=jobs.filter(j=>['queued','running'].includes(j.state));
  const activeTitle=filter.startsWith('c:')?bootstrap?.collections.find(c=>c.id===filter.slice(2))?.name||'集合':filter.startsWith('t:')?'标签 · '+filter.slice(2):({all:'我的文献库',starred:'星标文献',unread:'待读',reading:'在读',read:'已读',trash:'回收站',notes:'所有笔记'}[filter]||'文献库');
  if(exited)return <div className="boot-screen"><img className="brand-symbol" src="/icon.svg" alt=""/><h1>阅读器已退出</h1><p className="small muted">双击文献阅读器快捷方式即可重新打开。</p></div>;
  if(!bootstrap)return <div className="boot-screen"><img className="brand-symbol" src="/icon.svg" alt=""/><h1>文献阅读器</h1>{fatal?<><ErrorLine text={fatal}/><button className="button" onClick={refresh}>重新连接</button></>:<Busy text="打开本机文献库"/>}</div>;
  return <><input type="file" accept=".pdf,application/pdf" multiple ref={fileInput} hidden onChange={e=>{if(e.target.files)void importFiles(e.target.files)}}/>
  {readerId&&readerPaper&&<ReaderScreen key={readerId} paper={readerPaper} initialAnnotation={readerAnnotation}
    bootstrap={bootstrap} jobs={jobs} refreshKey={revision} returnPoint={referenceReturn}
    returnTitle={previousReference?.title} navigatingReference={navigatingReference}
    onFollowReference={navigateReference} onReturnReference={()=>{if(previousReference)void navigateReference(previousReference.paperId)}}
    onBack={locator=>{
      if (locator) {
        setPapers(items=>items.map(p=>p.id===readerId?{...p,locator}:p));
        setSelected(p=>p?.id===readerId?{...p,locator}:p);
        setNotesPaper(p=>p?.id===readerId?{...p,locator}:p);
      }
      setReaderId(null);
      setReaderAnnotation(null);
      resetReferenceTrail();
      refresh();
    }}
    onRefresh={refresh} onEdit={()=>{setSelected(readerPaper);setEditingPaper(true)}}
    onSettings={()=>setShowSettings(true)} onJobs={()=>setShowJobs(true)} notify={notify}/>}
  <div className="app-shell" hidden={!!readerId} onDragEnter={e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();dragDepth.current++;setDragging(true)}}} onDragLeave={e=>{if(e.dataTransfer.types.includes('Files')){dragDepth.current--;if(dragDepth.current<=0)setDragging(false)}}} onDragOver={e=>{if(e.dataTransfer.types.includes('Files'))e.preventDefault()}} onDrop={e=>{if(e.dataTransfer.files.length){e.preventDefault();dragDepth.current=0;setDragging(false);void importFiles(e.dataTransfer.files)}}}>
    <aside className="sidebar"><div className="brand"><img className="brand-symbol" src="/icon.svg" alt=""/><span>文献阅读器</span></div><div className="sidebar-content"><div className="sidebar-section"><button className={'nav-item '+(filter==='all'?'selected':'')} onClick={()=>chooseFilter('all')}><Library size={17}/><span>我的文献库</span><small>{bootstrap.counts.all||0}</small></button><button className={'nav-item '+(filter==='starred'?'selected':'')} onClick={()=>chooseFilter('starred')}><Star size={17}/><span>星标文献</span><small>{bootstrap.counts.starred||0}</small></button><button className={'nav-item '+(filter==='notes'?'selected':'')} aria-label="所有笔记" title="所有笔记" onClick={()=>chooseFilter('notes')}><Highlighter size={17}/><span>所有笔记</span><small>{bootstrap.counts.notes||''}</small></button></div><div className="sidebar-label">阅读状态</div><div className="sidebar-section status-nav">{[['unread',<Inbox size={16}/>],['reading',<BookOpen size={16}/>],['read',<Check size={16}/>]].map(([key,icon])=><button key={String(key)} className={'nav-item '+(filter===key?'selected':'')} onClick={()=>chooseFilter(String(key))}>{icon}<span>{STATUS_LABELS[String(key)]}</span><small>{bootstrap.counts[String(key)]||0}</small></button>)}</div><div className="sidebar-label"><span>集合</span><IconButton label="新建集合" onClick={()=>setCollectionDialog('')}><Plus size={14}/></IconButton></div><div className="sidebar-section collection-tree">{collectionTree()}{!bootstrap.collections.length&&<button className="empty-collection" onClick={()=>setCollectionDialog('')}><FolderPlus size={16}/>创建第一个集合</button>}</div><div className="sidebar-label">标签</div><div className="sidebar-tags">{(moreTags?bootstrap.tags:bootstrap.tags.slice(0,7)).map(t=><button key={t.name} className={filter==='t:'+t.name?'selected':''} onClick={()=>chooseFilter('t:'+t.name)} onContextMenu={e=>{e.preventDefault();setTagDialog(t.name)}}><span className="tag-dot"/>{t.name}<small>{t.count}</small></button>)}{!bootstrap.tags.length&&<p className="sidebar-hint">导入文献后添加研究主题标签</p>}{bootstrap.tags.length>7&&<button className="more-tags" onClick={()=>setMoreTags(!moreTags)}>{moreTags?'收起标签':`查看全部 ${bootstrap.tags.length} 个标签`}</button>}</div></div><div className="sidebar-bottom"><button className={'nav-item '+(filter==='trash'?'selected':'')} onClick={()=>chooseFilter('trash')}><Trash2 size={16}/><span>回收站</span><small>{bootstrap.counts.trash||''}</small></button><button className="nav-item" onClick={()=>setShowJobs(true)}>{activeJobs.length?<LoaderCircle size={16} className="spin"/>:<Clock3 size={16}/>}<span>任务与进度</span>{activeJobs.length>0&&<small className="count-badge">{activeJobs.length}</small>}</button><button className="nav-item" onClick={()=>setShowSettings(true)}><Settings2 size={16}/><span>设置</span></button><div className="local-indicator"><span className="status-dot"/>本机文献库</div></div></aside>
    <div className="main-workspace"><header className="library-topbar"><div className="search-field"><Search size={17}/><input ref={searchInput} aria-label={filter==='notes'?'搜索所有笔记':'搜索文献库'} value={query} onChange={e=>changeSearch(e.target.value)} placeholder={filter==='notes'?'搜索论文、笔记或摘录':'搜索标题、作者、正文或笔记'}/>{query?<IconButton label="清空搜索" onClick={()=>changeSearch('')}><X size={14}/></IconButton>:<kbd>Ctrl F</kbd>}</div><div className="topbar-actions"><span className="library-save-state"><span className="status-dot"/>本地保存</span><button className="button primary" disabled={uploading} onClick={()=>fileInput.current?.click()}>{uploading?<LoaderCircle size={16} className="spin"/>:<Plus size={17}/>}<span>{uploading?'正在导入':'导入 PDF'}</span></button></div></header>
    <div className="library-columns">{filter === 'notes' ? <main className="library-main">
      <NotesOverview groups={noteGroups} total={total} query={effectiveQuery} loading={loading||query.trim()!==effectiveQuery} error={notesError}
        hidden={!!notesWorkspace} listRef={noteGroupList} page={limit/paperPageSize} pageSize={paperPageSize}
        onRetry={refresh} onPage={page=>{setLimit(page*paperPageSize);if(noteGroupList.current)noteGroupList.current.scrollTop=0;}}
        onSelect={group=>{setNotesPaperError('');setNotesWorkspace({paperId:group.paper_id,query:effectiveQuery});}}/>
      {notesWorkspace&&<div className="notes-workspace">
        <button className="text-button notes-back" onClick={closePaperNotes}><ArrowLeft size={16}/>返回所有笔记</button>
        {notesPaperError ? <div className="library-error"><ErrorLine text={notesPaperError}/><button className="text-button" onClick={refresh}>重试</button></div>
          : notesPaper?.id===notesWorkspace.paperId ? <PaperNotes key={notesPaper.id} paper={notesPaper}
            initialQuery={notesWorkspace.query} revision={revision} onChange={refresh} notify={notify}
            onOpenAnnotation={note=>openReader(notesPaper,note)} onOpenPaper={()=>openReader(notesPaper)}/>
          : <Busy text="正在读取本篇笔记"/>}
      </div>}
    </main> : <><main className="library-main"><div className="library-titlebar"><div><h1>{effectiveQuery?'搜索结果':activeTitle}</h1><span className="library-count">{total} 篇文献</span></div><div className="library-view-tools"><select aria-label="文献排序" value={sortValue(sort)} onChange={event => {
      const next = librarySortOptions.find(option => sortValue(option) === event.target.value);
      if (next) changeSort(next);
    }}>
      {librarySortOptions.filter(option=>['updated','created','title',sort.field].includes(option.field)
        || columns.some(column=>column===option.field)).map(option =>
        <option key={sortValue(option)} value={sortValue(option)}>{option.label}</option>)}
    </select>
      <button type="button" className="button compact library-columns-button" aria-label="自定义表头" title="自定义表头"
        onClick={()=>setShowColumns(true)}><Columns3 size={15}/><span>表头</span></button>
      {filter.startsWith('t:')&&<IconButton label="管理当前标签" onClick={()=>setTagDialog(filter.slice(2))}><Tag size={15}/></IconButton>}
      <IconButton label="切换文献信息侧栏" active={details} onClick={()=>setDetails(!details)}><PanelRight size={17}/></IconButton>
    </div></div>
    {filter==='trash' && (bootstrap.counts.trash??0)>0 && <div className="trash-actions" role="group" aria-label="回收站操作">
      <button className="button" onClick={()=>setTrashAction({action:'restore'})}><Undo2 size={14}/>恢复全部</button>
      <button className="button danger" onClick={()=>setTrashAction({action:'purge'})}><Trash2 size={14}/>清空回收站</button>
    </div>}
    {filter!=='trash'&&batchPapers.length>0&&<div className="batch-actions" role="group" aria-label="已选文献操作">
      <strong role="status">已选 {batchPapers.length} 篇</strong>
      <button type="button" className="button primary compact" disabled={!canBatch}
        onClick={()=>setBatchDialog({action:'translate',papers:batchPapers})}><Languages size={15}/>批量翻译</button>
      <button type="button" className="button compact" disabled={!canBatch}
        onClick={()=>setBatchDialog({action:'metadata',papers:batchPapers})}><Globe2 size={15}/>联网补全</button>
      <select aria-label="批量设置阅读状态" value="" disabled={!canBatch} onChange={event=>{
        const status = event.target.value as Paper['status'];
        if (status) void markBatch(status);
      }}>
        <option value="" disabled>设为阅读状态</option>
        {Object.entries(STATUS_LABELS).map(([status,label])=><option key={status} value={status}>{label}</option>)}
      </select>
      <button type="button" className="text-button" disabled={batchUpdating} onClick={()=>setChecked(new Set())}>取消选择</button>
    </div>}
    {fatal&&<div className="library-error"><ErrorLine text={fatal}/><button className="text-button" onClick={refresh}>重试</button></div>}
    <div className="library-table-wrap" ref={libraryTable}>{papers.length?<>
      <LibraryTable papers={papers} columns={columns} sort={sort} focused={selectedId} checked={checked}
        selectable={filter!=='trash'} onSort={changeSort} onColumns={()=>setShowColumns(true)}
        onFocus={setSelectedId} onCheck={setChecked} onOpen={openReader}
        onStar={paper=>{void patch('/papers/'+paper.id,{starred:!paper.starred}).then(refresh).catch(e=>notify(e.message));}}/>
      <div className="table-pagination">
        <button className="button" disabled={limit<=paperPageSize||loading} onClick={()=>changePaperPage(limit-paperPageSize)}>上一页</button>
        <span>第 {limit/paperPageSize} / {Math.max(1,Math.ceil(total/paperPageSize))} 页</span>
        <button className="button" disabled={limit>=total||loading} onClick={()=>changePaperPage(limit+paperPageSize)}>下一页</button>
      </div>
    </>:loading?<Busy text="正在读取文献库"/>:<Empty icon={effectiveQuery?<Search size={34}/>:filter==='trash'?<Trash2 size={34}/>:<BookOpen size={35}/>} title={effectiveQuery?'没有找到匹配的文献':filter==='trash'?'回收站为空':filter==='all'?'从第一篇文献开始':'这个集合还没有文献'} text={effectiveQuery?'试试标题、作者、原文关键词或笔记中的文字。':filter==='trash'?'移入回收站的文献可以恢复。':'拖入 PDF，自动生成 HTML 阅读版。翻译、笔记和原文会保存在同一个文献库。'}>{!effectiveQuery&&filter!=='trash'&&<><button className="button primary" onClick={()=>fileInput.current?.click()}><Upload size={16}/>导入 PDF 文献</button><p className="empty-shortcut">支持批量导入 · 或按 Ctrl + O</p></>}</Empty>}</div>
    <footer className="library-footer"><span>{effectiveQuery?`正在搜索“${effectiveQuery}”`:'双击文献开始阅读，或拖到左侧集合整理'}</span><span>{total?`${limit-99}–${Math.min(limit,total)}`:0} / {total}</span></footer></main>{details&&<aside className="library-detail"><div className="detail-pane-header"><span>文献信息</span><IconButton label="关闭文献信息" onClick={()=>setDetails(false)}><X size={15}/></IconButton></div><div className="detail-scroll"><InfoPanel paper={selected} bootstrap={bootstrap} onEdit={()=>setEditingPaper(true)} onChange={refresh} onOpen={()=>{if(selected)openReader(selected)}} onNotes={selected?()=>openPaperNotes(selected):undefined} onPurge={selected?.deleted_at?()=>setTrashAction({action:'purge',paper:selected}):undefined} onDeleted={()=>{setSelectedId(null);refresh();notify('已移入回收站，可随时恢复')}}/></div></aside>}</>}</div></div>{dragging&&<div className="file-drop-overlay"><div><Upload size={34}/><h2>放开即可导入 PDF</h2><p>保留原件，并自动创建 HTML 阅读版本</p></div></div>}</div>
    {contextCollection&&<div className="collection-menu" role="menu"><strong>{contextCollection.name}</strong><button onClick={()=>{setCollectionDialog(contextCollection.id);setContextCollection(null)}}><FolderPlus size={14}/>新建子集合</button><button onClick={()=>{const c=contextCollection;setPrompt({title:'重命名集合',value:c.name,save:v=>patch('/collections/'+c.id,{name:v})});setContextCollection(null)}}><FileText size={14}/>重命名</button><button onClick={()=>{setMovingCollection(contextCollection);setContextCollection(null)}}><FolderInput size={14}/>移动集合</button><button onClick={()=>{const c=contextCollection;setPrompt({title:'移除集合',value:c.name,save:async()=>{await remove('/collections/'+c.id);notify('集合已移除，文献和笔记仍保留')}});setContextCollection(null)}}><Trash2 size={14}/>移除集合</button><button className="muted" onClick={()=>setContextCollection(null)}>关闭</button></div>}
    {toast&&<div className="toast" role="status"><span>{toast}</span><button aria-label="关闭提示" onClick={()=>setToast('')}><X size={15}/></button></div>}{showSettings&&<SettingsDialog onQuit={()=>setExited(true)} bootstrap={bootstrap} jobs={jobs} onClose={()=>setShowSettings(false)} onDone={refresh} onRestored={()=>{resetReferenceTrail();setReaderId(null);setSelectedId(null);setFilter('all');refresh()}} onJobs={refresh}/>} {showJobs&&<JobsDialog jobs={jobs} onClose={()=>setShowJobs(false)} onChange={refresh}/>} {collectionDialog!==false&&<CollectionDialog collections={bootstrap.collections} parent={collectionDialog||undefined} onClose={()=>setCollectionDialog(false)} onDone={refresh}/>} {editingPaper&&(readerPaper||selected)&&<PaperDialog paper={readerId&&readerPaper?readerPaper:selected!} collections={bootstrap.collections} onClose={()=>setEditingPaper(false)} onDone={refresh}/>} {movingCollection&&<CollectionDialog collections={bootstrap.collections} move={movingCollection}
      onClose={()=>setMovingCollection(null)} onDone={parent=>{
        if (parent) setCollapsed(old=>{const next=new Set(old);next.delete(parent);return next;});
        refresh();notify('集合已移动');
      }}/>}
    {tagDialog&&<TagDialog tag={tagDialog} onClose={()=>setTagDialog(null)} onDone={finishTag}/>}
    {trashAction&&<TrashDialog {...trashAction} onClose={()=>setTrashAction(null)} onDone={finishTrash}/>}
    {showColumns&&<LibraryColumnsDialog columns={columns} onClose={()=>setShowColumns(false)} onChange={next=>{
      setColumns(next);
      rememberLibraryColumns(next);
    }}/>}
    {batchDialog&&<BatchJobsDialog {...batchDialog} bootstrap={bootstrap} onClose={()=>setBatchDialog(null)}
      onQueued={acceptQueuedBatch} onJobs={()=>{setBatchDialog(null);setShowJobs(true);}}/>}
    {prompt&&<PromptDialog prompt={prompt} onClose={()=>setPrompt(null)} onDone={refresh}/>}
  </>
}

function PromptDialog({prompt,onClose,onDone}:{prompt:{title:string;value:string;save:(v:string)=>Promise<unknown>};onClose:()=>void;onDone:()=>void}){const [value,setValue]=useState(prompt.value),[error,setError]=useState(''),[busy,setBusy]=useState(false);const deleting=prompt.title==='移除集合';async function save(){setBusy(true);try{await prompt.save(value);onDone();onClose()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}return <Modal title={prompt.title} onClose={onClose} footer={<><button className="button" onClick={onClose}>取消</button><button className={'button '+(deleting?'danger':'primary')} disabled={busy||!value.trim()} onClick={save}>{deleting?'移除集合':'保存'}</button></>}>{deleting?<p>移除“{value}”？其中的文献仍留在文献库中，子集合会移到上一级。</p>:<label className="field">集合名称<input value={value} onChange={e=>setValue(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')void save()}}/></label>}<ErrorLine text={error}/></Modal>}

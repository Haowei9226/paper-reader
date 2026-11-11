import {useCallback,useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';
import {ArrowLeft,BookOpen,Clock3,ChevronDown,ChevronLeft,ChevronRight,Columns2,Download,FileText,Languages,Link2,List,LoaderCircle,PanelRight,RotateCw,Rows3,Search,Settings2,Sparkles,Unlink,X} from 'lucide-react';
import {api,patch,post} from './api';
import {Busy,ErrorLine,IconButton,Modal,PublicationBadge} from './components';
import {AlignmentDialog,NoteDialog,OcrDialog,TranslationDialog} from './Dialogs';
import ReferencesPanel, {type ReferenceFocus} from './ReferencesPanel';
import FindPanel from './FindPanel';
import HtmlStream from './HtmlStream';
import HtmlView, {indexAnnotations, readableText} from './HtmlView';
import {AIPanel,InfoPanel,NotesPanel} from './Panels';
import PdfView from './PdfView';
import SelectionTools, {useCounterpart} from './SelectionTools';
import {PROVIDERS,PROVIDER_NAMES} from './types';
import type {AIResult,Annotation,Block,Bootstrap,FindHit,Job,Layout,Locator,PageData,Paper,Provider,ReadingMode,ReadingSide,ReferenceAnchor,ReferenceIndex,Selection} from './types';
import {useReadingPosition} from './useReadingPosition';
import type {Move,Position} from './readerPosition';
import {useHtmlPages} from './useHtmlPages';

type Task = {kind: 'translate' | 'summary'; page: number};
export type ReferenceReturn = {
  paperId: string;
  title: string;
  position: Position;
  layout: Layout;
  singleSide: ReadingSide;
  sync: boolean;
  showToc: boolean;
  focus: ReferenceFocus | null;
};
const emptyReferences:ReferenceIndex = {entries:[],heading:null};
const readerAnnotationLimit = 3000;

function annotationMove(annotation: Annotation | null, paper: Paper): Move | undefined {
  if (annotation?.paper_id !== paper.id) return;
  const target = annotation.targets.find(({page}) => page && page >= 1 && page <= paper.pages);
  if (!target?.page) return;
  const {page, block_id, start, end, quote} = target;
  return {
    position: {page, block_id, side: 'source'},
    focus: true,
    hit: quote ? {page, block_id, start, end, side: 'source', translation_id: null, before: '', match: quote, after: ''} : undefined,
  };
}

type ReaderScreenProps = {
  paper: Paper;
  bootstrap: Bootstrap;
  jobs: Job[];
  refreshKey: number;
  initialAnnotation?: Annotation | null;
  returnPoint?: ReferenceReturn;
  returnTitle?: string;
  navigatingReference: boolean;
  onFollowReference: (paperId: string, from: ReferenceReturn) => void;
  onReturnReference: () => void;
  onBack: (locator?: Locator) => void;
  onRefresh: () => void;
  onEdit: () => void;
  onSettings: () => void;
  onJobs: () => void;
  notify: (message: string) => void;
};

export default function ReaderScreen({paper,bootstrap,jobs,refreshKey,initialAnnotation=null,returnPoint,returnTitle,navigatingReference,
  onFollowReference,onReturnReference,onBack,onRefresh,onEdit,onSettings,onJobs,notify}:ReaderScreenProps){
  const initialNote = initialAnnotation?.paper_id === paper.id ? initialAnnotation : null;
  const initialMove = returnPoint ? {position:returnPoint.position} : annotationMove(initialNote, paper);
  const clampPage = (p: number) => Math.max(1, Math.min(paper.pages || 1, p));
  const [page,setPage]=useState(()=>initialMove?.position.page??clampPage(paper.locator.page||1)),[pagedData,setPagedData]=useState<PageData|null>(null);
  const [anchorPage,setAnchorPage]=useState(page);
  const [layout,setLayout]=useState<Layout>(returnPoint?.layout??bootstrap.settings.layout);
  const [sourceMode,setSourceMode]=useState<ReadingMode>(()=>layout==='interleaved'?'html':returnPoint?.position.mode??paper.locator.mode??'pdf');
  const [singleSide,setSingleSide]=useState<ReadingSide>(()=>returnPoint?.singleSide??initialMove?.position.side??paper.locator.side??'source'),[pdfLayout,setPdfLayout]=useState(0),[findFocus,setFindFocus]=useState(0);
  const [selectedHeading,setSelectedHeading]=useState<number|null>(null);
  const [showRight,setShowRight] = useState(() => !!returnPoint || window.innerWidth > 1050 || (!!initialNote && !initialMove));
  const [annotations,setAnnotations] = useState<Annotation[]>(() => initialNote ? [initialNote] : []);
  const [selectedAnnotation,setSelectedAnnotation] = useState<string|null>(() => initialNote?.id ?? null);
  const [showToc,setShowToc] = useState(returnPoint?.showToc ?? false);
  const [sync,setSync] = useState(returnPoint?.sync ?? true);
  const [fontSize,setFontSize]=useState(bootstrap.settings.font_size),[provider,setProvider]=useState<Provider>(bootstrap.settings.provider),[results,setResults]=useState<AIResult[]>([]),[selection,setSelection]=useState<Selection|null>(null),[noteOpen,setNoteOpen]=useState(false),[noteSelection,setNoteSelection]=useState<Selection|null>(null),[aligning,setAligning]=useState<Annotation|null>(null),[editingTranslation,setEditingTranslation]=useState<Block|null>(null),[task,setTask]=useState<Task|null>(null),[submitting,setSubmitting]=useState(false),[question,setQuestion]=useState(''),[pageInput,setPageInput]=useState<string|null>(null);
  const [rightTab,setRightTab] = useState<'info'|'notes'|'ai'|'find'|'references'>(returnPoint?'references':'notes');
  const [references,setReferences] = useState<ReferenceIndex|null>(null);
  const [referenceError,setReferenceError] = useState('');
  const [referenceFocus,setReferenceFocus] = useState<ReferenceFocus|null>(returnPoint?.focus??null);
  useEffect(() => {
    setReferenceError('');
    if (!paper.blocks_count) {setReferences(emptyReferences);return}
    const request = new AbortController();
    void api<ReferenceIndex>(`/papers/${paper.id}/references`,{signal:request.signal}).then(index => {
      if (!request.signal.aborted) setReferences(index);
    }).catch(error => {
      if (!request.signal.aborted) setReferenceError((error as Error).message);
    });
    return () => request.abort();
  }, [paper.id,paper.blocks_count,paper.parse_state,refreshKey]);
  const openCitation = useCallback((numbers:number[]) => {
    if (!numbers.length) return;
    setReferenceFocus(previous => ({numbers,active:numbers[0],request:(previous?.request ?? 0) + 1}));
    setRightTab('references');
    setShowRight(true);
  }, []);
  const citationLinks = useMemo(() => ({
    known:new Set(references?.entries.filter(entry=>entry.numbered!==false).map(entry=>entry.number)),
    entries:references?.entries??[],
    open:openCitation,
  }), [references,openCitation]);
  useEffect(()=>setProvider(bootstrap.settings.provider),[bootstrap.settings.provider]);
  const sourcePane=useRef<HTMLDivElement>(null),targetPane=useRef<HTMLDivElement>(null);
  const pdf=sourceMode==='pdf'&&(layout==='parallel'||(layout==='single'&&singleSide==='source'));
  const localRefresh=useRef(0);const [localKey,setLocalKey]=useState(0);
  const revision=`${refreshKey}:${localKey}`;
  const flow=useHtmlPages({paperId:paper.id,total:Math.max(1,paper.pages),enabled:!pdf&&paper.pages>0,anchor:anchorPage,visible:page,revision});
  const data=pdf?(pagedData?.number===page?pagedData:null):flow.target;
  const onPageVisible=useCallback((visible:number)=>{setPage(visible);setSelectedHeading(null)},[]);
  const position=useReadingPosition({paper,page,mode:sourceMode,layout,singleSide,data,pdf,pdfLayout,sync,sourcePane,targetPane,onPageVisible,initialMove});
  const counterpart=useCounterpart(paper.id,selection,pdf),preview=counterpart?.data;
  const place=position.place;
  useLayoutEffect(()=>{
    if(layout!=='parallel'||!sync||!preview?.ranges.length)return;
    const pane=preview.side==='source'?sourcePane.current:targetPane.current;
    if(!pane)return;
    const rect=[...pane.querySelectorAll('[data-selection-match]')].map(el=>el.getBoundingClientRect()).find(r=>r.height>0);
    const viewport=pane.getBoundingClientRect();
    if(rect&&(rect.top>=viewport.bottom-10||rect.bottom<=viewport.top+10))place(pane,pane.scrollTop+rect.top-viewport.top-Math.min(90,pane.clientHeight*.2));
  },[layout,sync,preview,pdfLayout,place]);
  const onPdfLayout=useCallback(()=>setPdfLayout(value=>value+1),[]);
  const refresh=()=>{setLocalKey(++localRefresh.current);onRefresh()};
  useEffect(()=>{if(!pdf)return;let active=true;void api<PageData>(`/papers/${paper.id}/pages/${page}`).then(v=>{if(active)setPagedData(v)}).catch(()=>{if(active)setPagedData(null)});return()=>{active=false}},[paper.id,page,pdf,revision]);
  const initialNoteId = initialNote?.id;
  useEffect(() => {
    let active = true;
    void Promise.all([
      api<Annotation[]>(`/papers/${paper.id}/annotations?limit=${readerAnnotationLimit}`),
      api<AIResult[]>(`/papers/${paper.id}/ai-results`),
    ]).then(async ([notes, results]) => {
      if (!active) return;
      // A note opened from the library may be older than the reader's annotation window.
      if (initialNoteId && notes.length === readerAnnotationLimit && !notes.some(note => note.id === initialNoteId)) {
        const note = await api<Annotation>(`/annotations/${initialNoteId}`).catch(() => null);
        if (note) notes.push(note);
      }
      if (active) {
        setAnnotations(notes);
        setResults(results);
      }
    }).catch(error => {if (active) notify(error.message)});
    return () => {active = false};
  }, [paper.id, refreshKey, localKey, initialNoteId]);
  useEffect(()=>{
    const close=()=>{const active=window.getSelection();if(!active||active.isCollapsed||!active.toString().trim())setSelection(null)};
    document.addEventListener('selectionchange',close);
    return()=>document.removeEventListener('selectionchange',close);
  },[]);
  const openFind=useCallback(()=>{setRightTab('find');setShowRight(true);setFindFocus(value=>value+1)},[]);
  useEffect(()=>{
    const keyboard=(event:KeyboardEvent)=>{
      if(document.querySelector('[aria-modal="true"]'))return;
      if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='f'){event.preventDefault();openFind()}
    };
    document.addEventListener('keydown',keyboard);
    return()=>document.removeEventListener('keydown',keyboard);
  },[openFind]);
  function changePage(next:number){if(next!==page)setSelectedHeading(null);setPage(next);setAnchorPage(next);setSelection(null);window.getSelection()?.removeAllRanges()}
  function go(p:number){
    const next=clampPage(p);
    if(next===page)return;
    position.navigate({position:{page:next,side:layout==='single'?singleSide:'source'}});
    changePage(next);
  }
  function commitPageInput(){
    if(pageInput===null)return;
    const entered=Number(pageInput);
    setPageInput(null);
    if(Number.isInteger(entered)&&entered>0)go(entered);
  }
  function jump(p:number,blockId?:string,heading?:string,hit?:FindHit){
    const next=clampPage(p),side=hit?.side??'source';
    if(layout==='single')setSingleSide(side);
    position.navigate({position:{page:next,block_id:blockId,side},focus:true,heading,hit});
    changePage(next);
  }
  function jumpToReference(anchor:ReferenceAnchor){
    jump(anchor.page,anchor.block_id,undefined,{...anchor,side:'source',translation_id:null,before:'',match:anchor.quote,after:''});
    if(window.innerWidth<=1050)setShowRight(false);
  }
  function changeView(nextLayout:Layout,nextMode:ReadingMode,nextSide:ReadingSide){
    if(nextLayout===layout&&nextMode===sourceMode&&nextSide===singleSide)return;
    const location=position.current();
    position.navigate({position:{...location,side:nextLayout==='single'?nextSide:location.side}});
    changePage(location.page);
    setLayout(nextLayout);setSourceMode(nextMode);setSingleSide(nextSide);
    if(nextLayout!==layout)void patch('/settings',{layout:nextLayout}).catch(e=>notify(e.message));
  }
  function back(){void position.save().then(onBack)}
  function followReference(paperId:string,number:number) {
    const focus = {
      numbers:referenceFocus?.numbers.includes(number) ? referenceFocus.numbers : [number],
      active:number,
      request:(referenceFocus?.request ?? 0)+1,
    };
    setReferenceFocus(focus);
    onFollowReference(paperId,{
      paperId:paper.id,title:paper.title,position:position.current(),layout,singleSide,sync,showToc,focus,
    });
    void position.save();
  }
  function returnToReference() {
    onReturnReference();
    void position.save();
  }
  const selectAnnotation=useCallback((a:Annotation)=>{setSelectedAnnotation(a.id);setRightTab('notes');setShowRight(true)},[]);
  function openAnnotation(annotation: Annotation) {
    selectAnnotation(annotation);
    const move = annotationMove(annotation, paper);
    if (!move) return;
    jump(move.position.page, move.position.block_id, undefined, move.hit);
    if (window.innerWidth <= 1050) setShowRight(false);
  }
  const openTranslation=useCallback((number:number)=>setTask({kind:'translate',page:number}),[]);
  async function saveAnnotation(note='',color='yellow',s:Selection|null=selection){const a=await post<Annotation>(`/papers/${paper.id}/annotations`,{targets:s?.targets||[],translated_targets:s?.translated_targets||[],selected_language:s?.side||'source',note,color});setAnnotations(old=>[a,...old]);setSelection(null);window.getSelection()?.removeAllRanges();selectAnnotation(a);onRefresh();return a}
  function newNote(s:Selection|null){setNoteSelection(s);setNoteOpen(true);setSelection(null);window.getSelection()?.removeAllRanges()}
  async function startJob(payload:Record<string,unknown>){setSubmitting(true);try{await post(`/papers/${paper.id}/jobs`,{provider,...payload});refresh();notify('任务已加入队列，完成的内容会自动保存')}catch(e){notify((e as Error).message);throw e}finally{setSubmitting(false)}}
  function setModel(value:Provider){setProvider(value);void patch('/settings',{provider:value}).then(onRefresh).catch(e=>notify(e.message))}
  async function explain(s:Selection,translate=false){
    const block_ids=Array.from(new Set([...s.targets,...s.translated_targets].map(t=>t.block_id)));
    setRightTab('ai');setShowRight(true);setSelection(null);window.getSelection()?.removeAllRanges();
    const toEnglish=(paper.language==='zh')!==(s.side==='translation');
    const task=translate
      ? {task:'translate_selection',question:readableText(s.text),target_language:toEnglish?'en':'zh'}
      : {task:'explain',question:`请结合文献上下文解释以下选中文字，并说明其含义：\n${s.text}`};
    try{await startJob({kind:'reading',block_ids,...task})}catch{}
  }
  const currentJobs=jobs.filter(j=>j.paper_id===paper.id&&['queued','running'].includes(j.state));
  const [rebuildStarting,setRebuildStarting]=useState(false);
  const [rebuildId,setRebuildId]=useState<string|null>(null);
  const requestedRebuild=jobs.find(job=>job.id===rebuildId);
  const awaitingRebuild=!!rebuildId&&!requestedRebuild;
  const rebuilding=rebuildStarting||awaitingRebuild||currentJobs.some(job=>job.kind==='parse'&&job.payload.rebuild===true);
  const latestRebuild=jobs.find(job=>job.paper_id===paper.id&&job.kind==='parse'&&job.payload.rebuild===true);

  useEffect(()=>{
    if (!requestedRebuild||['queued','running'].includes(requestedRebuild.state)) return;
    setRebuildId(null);
    if (requestedRebuild.state==='done') {
      setSelection(null);
      window.getSelection()?.removeAllRanges();
    }
    if (['done','failed'].includes(requestedRebuild.state)) notify(requestedRebuild.message);
  },[requestedRebuild,notify]);

  async function startRebuild() {
    if (rebuilding||currentJobs.length||paper.deleted_at) return;
    setRebuildStarting(true);
    try {
      const job=await post<Job>(`/papers/${paper.id}/jobs`,{kind:'parse',rebuild:true});
      setRebuildId(job.id);
      notify('已加入重新转换队列');
    } catch (error) {
      notify((error as Error).message);
    } finally {
      setRebuildStarting(false);
    }
  }

  const [ocrOpen,setOcrOpen] = useState(false);
  const ocrPending = paper.ocr_pending_pages ?? 0;
  const ocrEmpty = paper.ocr_empty_pages ?? 0;
  const ocrRunning = currentJobs.some(job => job.kind==='ocr');
  const canStartOcr = ocrPending>0 && !ocrRunning && !rebuilding && !paper.deleted_at &&
    ['ready','no_text'].includes(paper.parse_state);
  const latestTranslation=jobs.filter(j=>j.paper_id===paper.id&&j.kind==='translate').sort((a,b)=>b.updated-a.updated)[0];
  const translationNeedsAttention=latestTranslation&&['failed','partial'].includes(latestTranslation.state);
  const annotationsByBlock=useMemo(()=>indexAnnotations(annotations),[annotations]);
  const htmlReaderProps = {
    paper,
    annotations: annotationsByBlock,
    onSelect: setSelection,
    onAnnotation: selectAnnotation,
    onEdit: setEditingTranslation,
    fontSize,
    flash: position.flash,
    hit: position.hit,
    preview,
    onTranslate: openTranslation,
    citations: citationLinks,
  };
  const streamProps={...htmlReaderProps,pages:flow.pages,anchorPage,viewportKey:`${showToc}:${showRight}`,canPrepend:flow.canPrepend,canAppend:flow.canAppend,extend:flow.extend,retry:flow.retry,place:position.place};
  const originalPdf = <PdfView
    paperId={paper.id} pageNumber={page} data={data} annotations={annotations} hit={position.hit} preview={preview} onLayout={onPdfLayout}
    onSelect={setSelection} onAnnotation={selectAnnotation}
    citations={citationLinks}
  />;
  const hasText=paper.parse_state==='ready'||paper.parse_state==='processing';
  const currentHeading=selectedHeading??paper.outline.findIndex(entry=>entry.page===page);
  return <div className="reader-screen"><header className="reader-header">
    <button className="back-library" disabled={navigatingReference} onClick={back}><ArrowLeft size={17}/><span>文献库</span></button>
    {returnTitle && <button type="button" className="back-library reference-return" disabled={navigatingReference}
      title={`返回引用处 · ${returnTitle}`} onClick={returnToReference}><ArrowLeft size={15}/>返回引用处</button>}
    <span className="header-divider"/><FileText size={17} className="muted"/><PublicationBadge status={paper.publication_status}/><h1 title={paper.title}>{paper.title}</h1><div className="reader-header-actions"><IconButton label="任务与进度" onClick={onJobs}><Clock3 size={17}/></IconButton><IconButton label="导出带批注 PDF" onClick={()=>{window.location.href=`/api/papers/${paper.id}/export/pdf`}}><Download size={17}/></IconButton><IconButton label="设置" onClick={onSettings}><Settings2 size={17}/></IconButton></div></header>
  <div className="reader-toolbar"><div className="toolbar-cluster">
    <IconButton label="章节目录" active={showToc} onClick={()=>setShowToc(!showToc)}><List size={17}/></IconButton>
    <IconButton label="文内查找 (Ctrl+F)" active={showRight&&rightTab==='find'} onClick={openFind}><Search size={17}/></IconButton>
    <div className="segmented">
      <button className={sourceMode==='pdf'?'selected':''} title={layout==='interleaved'?'段落交替使用 HTML':undefined} disabled={layout==='interleaved'} onClick={()=>changeView(layout,'pdf','source')}>PDF</button>
      <button className={sourceMode==='html'?'selected':''} onClick={()=>changeView(layout,'html','source')}>HTML</button>
    </div>
    <button type="button" className="button compact rebuild-html" aria-label="重新转换 HTML"
      title={currentJobs.length&&!rebuilding?'请等当前文献任务完成':'重新转换 HTML'}
      disabled={rebuilding||currentJobs.length>0||!!paper.deleted_at} onClick={()=>void startRebuild()}>
      {rebuilding?<LoaderCircle size={14} className="spin"/>:<RotateCw size={14}/>}<span>重新转换 HTML</span>
    </button>
    <span className="toolbar-separator"/><div className="page-controls"><IconButton label="上一页" disabled={page<=1} onClick={()=>go(page-1)}><ChevronLeft size={16}/></IconButton><input aria-label="当前页码" inputMode="numeric" value={pageInput??String(page)} onChange={e=>setPageInput(e.target.value)} onBlur={commitPageInput} onKeyDown={e=>{if(e.key==='Enter')commitPageInput();else if(e.key==='Escape')setPageInput(null)}}/><span>/ {paper.pages||'…'}</span><IconButton label="下一页" disabled={page>=paper.pages} onClick={()=>go(page+1)}><ChevronRight size={16}/></IconButton></div></div>
  <div className="toolbar-cluster layout-controls"><div className="segmented icon-segmented">
    <button title="左右对照" aria-label="左右对照" className={layout==='parallel'?'selected':''} onClick={()=>changeView('parallel',sourceMode,singleSide)}><Columns2 size={16}/></button>
    <button title="段落交替" aria-label="段落交替" className={layout==='interleaved'?'selected':''} onClick={()=>changeView('interleaved','html',singleSide)}><Rows3 size={16}/></button>
    <button title="单语阅读" aria-label="单语阅读" className={layout==='single'?'selected':''} onClick={()=>changeView('single',sourceMode,singleSide)}><BookOpen size={16}/></button>
  </div><IconButton label={sync?'关闭同步滚动':'开启同步滚动'} active={sync&&layout==='parallel'} disabled={layout!=='parallel'} onClick={()=>{if(!sync)position.align();setSync(!sync)}}>{sync?<Link2 size={16}/>:<Unlink size={16}/>}</IconButton><select className="font-select" aria-label="阅读字号" value={fontSize} onChange={e=>{setFontSize(Number(e.target.value));void patch('/settings',{font_size:Number(e.target.value)})}}>{[15,17,19,21,24].map(v=><option key={v} value={v}>{v}px</option>)}</select></div>
  <div className="toolbar-cluster reader-task-controls"><select aria-label="翻译与 AI 模型" className="provider-select" value={provider} onChange={e=>setModel(e.target.value as Provider)}>{PROVIDERS.map(p=><option key={p} value={p}>{PROVIDER_NAMES[p]}</option>)}</select><button className="button primary compact" onClick={()=>openTranslation(page)} disabled={!hasText||submitting||rebuilding}>{submitting?<LoaderCircle className="spin" size={15}/>:<Languages size={15}/>}翻译<ChevronDown size={12}/></button><button className="button compact" onClick={()=>{setRightTab('ai');setShowRight(true)}}><Sparkles size={15}/>AI 阅读</button><IconButton label="切换阅读侧栏" active={showRight} onClick={()=>setShowRight(!showRight)}><PanelRight size={17}/></IconButton></div></div>
  {currentJobs.length>0&&<div className="reader-progress"><LoaderCircle size={13} className="spin"/><span>{currentJobs[0].message||(rebuilding?'等待重新转换 HTML…':'准备处理文献…')}</span>{currentJobs[0].total>0&&<progress value={currentJobs[0].progress} max={currentJobs[0].total}/>}</div>}
  {!currentJobs.length&&!rebuilding&&latestRebuild?.state==='failed'&&<div className="reader-progress reader-progress-attention">
    <span>{latestRebuild.message}</span><button className="text-button" onClick={onJobs}>查看转换任务</button>
  </div>}
  {currentJobs.length===0&&translationNeedsAttention&&<div className="reader-progress reader-progress-attention">
    <span>{latestTranslation.state==='partial'?'翻译部分完成':'翻译中断'} · {latestTranslation.message}</span>
    <button className="text-button" onClick={onJobs}>查看任务并继续</button>
  </div>}
  {canStartOcr&&<div className="reader-progress reader-progress-attention">
    <span>{ocrPending} 页无可提取文字{ocrEmpty>0&&`，其中 ${ocrEmpty} 页上次未识别到文字`}</span>
    <button className="text-button" onClick={()=>setOcrOpen(true)}>本地 OCR</button>
  </div>}
  <div className="reader-body">{showToc&&<aside className="outline-panel"><div className="panel-tools"><strong>文献目录</strong><IconButton label="收起目录" onClick={()=>setShowToc(false)}><X size={14}/></IconButton></div>{paper.outline.length?paper.outline.map((o,i)=><button key={i} className={i===currentHeading?'current':''} aria-current={i===currentHeading?'location':undefined} style={{paddingLeft:14+Math.min(o.level-1,3)*12}} onClick={()=>{jump(o.page,undefined,o.title);setSelectedHeading(i)}}><span>{o.title}</span><small>{o.page}</small></button>):<p className="muted small space-y">正文解析后会尝试识别章节标题。</p>}</aside>}
  <main className={`reading-area layout-${layout}`}>
    {layout === 'parallel' ? <>
      <section className="reading-pane">
        <div className="pane-heading">
          <span>{paper.language === 'zh' ? '中文原文' : '英文原文'}<small>{sourceMode === 'pdf' ? 'PDF 原件' : 'HTML 连续阅读'}</small></span>
          {(pdf||sync)&&<span className="small muted">第 {page} 页</span>}
        </div>
        <div className="pane-scroll source-scroll" ref={sourcePane} tabIndex={0} aria-label="原文阅读区" onScroll={() => position.trackScroll('source')}>
          {pdf ? originalPdf : paper.pages > 0 ? <HtmlStream {...streamProps} side="source"/> : <Busy text="等待正文解析"/>}
        </div>
      </section>
      <section className="reading-pane translated-pane">
        <div className="pane-heading">
          <span>{paper.language === 'zh' ? '英文译文' : '中文译文'}<small>与原文共享标注</small></span>
          <Languages size={14}/>
        </div>
        <div className="pane-scroll" ref={targetPane} tabIndex={0} aria-label="译文阅读区" onScroll={() => position.trackScroll('translation')}>
          {!pdf && paper.pages > 0 ? <HtmlStream {...streamProps} side="translation"/>
            : data ? <HtmlView {...htmlReaderProps} side="translation" page={page} blocks={data.blocks} tables={data.tables} onPageChange={go}/>
            : <Busy text="等待正文解析"/>}
        </div>
      </section>
    </> : <section className="reading-pane single-reading-pane">
      <div className="pane-heading">
        <span>{layout === 'interleaved' ? '原文与译文 · 段落交替' : '单语阅读'}</span>
        {layout === 'single' && <div className="segmented small-segmented">
          <button className={singleSide === 'source' ? 'selected' : ''} onClick={() => changeView(layout,sourceMode,'source')}>原文</button>
          <button className={singleSide === 'translation' ? 'selected' : ''} onClick={() => changeView(layout,sourceMode,'translation')}>译文</button>
        </div>}
      </div>
      <div className="pane-scroll" ref={sourcePane} tabIndex={0} aria-label="正文阅读区" onScroll={()=>position.trackScroll(layout==='single'?singleSide:'source')}>
        {pdf ? originalPdf : paper.pages > 0
          ? <HtmlStream {...streamProps} side={layout === 'interleaved' ? 'source' : singleSide} interleaved={layout === 'interleaved'}/>
          : <Busy text="等待正文解析"/>}
      </div>
    </section>}
  </main>{(showRight||rightTab==='find')&&<aside className="reader-right-panel" style={showRight?undefined:{display:'none'}}>
    <div className="panel-tabs">
      <button className={rightTab==='info'?'selected':''} onClick={()=>setRightTab('info')}>信息</button>
      <button className={rightTab==='notes'?'selected':''} onClick={()=>setRightTab('notes')}>笔记<span>{annotations.length}</span></button>
      <button className={rightTab==='ai'?'selected':''} onClick={()=>setRightTab('ai')}><Sparkles size={13}/>AI</button>
      <button className={rightTab==='find'?'selected':''} onClick={openFind}>查找</button>
      <button className={rightTab==='references'?'selected':''} onClick={()=>{setRightTab('references');setReferenceFocus(null)}}>参考文献</button>
      <IconButton label="收起侧栏" onClick={()=>{setShowRight(false);position.clearHit()}}><X size={15}/></IconButton>
    </div>
    <div className="reader-panel-scroll" key={rightTab}>
      {rightTab==='find'?<FindPanel paper={paper} focus={findFocus} revision={revision} activeHit={position.hit} onHit={hit=>{jump(hit.page,hit.block_id,undefined,hit);if(window.innerWidth<=1050)setShowRight(false)}} onClear={position.clearHit}/>
        :rightTab==='references'?<ReferencesPanel index={references} error={referenceError} focus={referenceFocus} onJump={jumpToReference}
          navigating={navigatingReference} onOpenPaper={followReference}
          onFocus={active=>setReferenceFocus(previous=>previous?{...previous,active}:{numbers:[active],active,request:1})}/>
        :rightTab==='info'?<InfoPanel paper={paper} bootstrap={bootstrap} onEdit={onEdit} onChange={refresh} onOpen={()=>{}} onDeleted={()=>onBack()}/>
        :rightTab==='notes'?<NotesPanel paper={paper} annotations={annotations} selectedId={selectedAnnotation} onChange={refresh} onNew={()=>newNote(null)} onOpenAnnotation={openAnnotation} onAlign={setAligning} onAIAlign={a=>{void startJob({kind:'align',annotation_id:a.id,selected_language:'source'}).catch(()=>{})}}/>
        :<AIPanel paper={paper} results={results} jobs={jobs} provider={provider} onProvider={setModel} onQuestion={()=>{void startJob({kind:'reading',task:'question',question,conversation_memory:true}).then(()=>setQuestion('')).catch(()=>{})}} onSummary={()=>setTask({kind:'summary',page})} onJump={jump} onSaved={refresh} question={question} setQuestion={setQuestion}/>}
    </div>
  </aside>}</div>
  <footer className="reader-status"><span><span className="status-dot"/>已保存在本机</span><span>{data?.ocr?'本页文字由本地 OCR 识别':paper.parse_state==='ready'?'基础版面解析 · 文字可重排':paper.parse_state==='no_text'?'此文件尚无可提取文字':'正文正在解析'} · {paper.annotations_count} 条笔记与标注</span><span>原件保留 · 译文按版本保存</span></footer>
  {ocrOpen&&<OcrDialog paper={paper} pages={ocrPending} onClose={()=>setOcrOpen(false)}
    onStarted={()=>{refresh();notify('本地 OCR 已加入任务队列')}}/>}
  {selection&&<SelectionTools selection={selection} counterpart={preview} error={counterpart?.error}
    onHighlight={()=>void saveAnnotation().catch(e=>notify(e.message))} onNote={()=>newNote(selection)}
    onTranslate={()=>void explain(selection,true)} onExplain={()=>void explain(selection)}
    onLocate={range=>{if(preview)jump(range.page,range.block_id,undefined,{...range,side:preview.side,translation_id:range.translation_id??null,before:'',match:range.quote,after:''})}}/>}
  {noteOpen&&<NoteDialog selection={noteSelection} onClose={()=>setNoteOpen(false)} onSave={async(note,color)=>{await saveAnnotation(note,color,noteSelection)}}/>}{aligning&&<AlignmentDialog annotation={aligning} onClose={()=>setAligning(null)} onDone={refresh}/>} {editingTranslation&&<TranslationDialog block={editingTranslation} onClose={()=>setEditingTranslation(null)} onDone={refresh}/>} {task&&<TaskDialog kind={task.kind} paper={paper} page={task.page} provider={provider} submitting={submitting} onClose={()=>setTask(null)} onRun={async(scope,force,preserve)=>{const payload:Record<string,unknown>={kind:task.kind==='translate'?'translate':'reading',...(task.kind==='summary'?{task:'summary'}:{neighbor_context:true}),force,preserve_manual:preserve};if(scope==='page'){payload.page_start=task.page;payload.page_end=task.page}else if(scope==='chapter'){const before=paper.outline.filter(o=>o.page<=task.page);const start=before.length?before[before.length-1].page:1;const next=paper.outline.find(o=>o.page>task.page);payload.page_start=start;payload.page_end=next?next.page-1:paper.pages}await startJob(payload);if(task.kind==='summary'){setRightTab('ai');setShowRight(true)}setTask(null)}}/>}
  </div>
}

function TaskDialog({kind,paper,page,provider,submitting,onClose,onRun}:{kind:'translate'|'summary';paper:Paper;page:number;provider:Provider;submitting:boolean;onClose:()=>void;onRun:(scope:string,force:boolean,preserve:boolean)=>Promise<void>}){
  const [scope,setScope]=useState('page'),[force,setForce]=useState(false),[preserve,setPreserve]=useState(true),[error,setError]=useState('');
  return <Modal title={kind==='translate'?'学术翻译':'生成结构化摘要'} onClose={()=>{if(!submitting)onClose()}} footer={<><button className="button" disabled={submitting} onClick={onClose}>取消</button><button className="button primary" disabled={submitting} onClick={()=>void onRun(scope,force,preserve).catch(e=>setError(e.message))}>{submitting?<LoaderCircle size={15} className="spin"/>:kind==='translate'?<Languages size={15}/>:<Sparkles size={15}/>}开始{kind==='translate'?'翻译':'生成'}</button></>}><div className="scope-options">{[['page',`当前页 · 第 ${page} 页`],['chapter','当前章节'],['all',`全文 · ${paper.pages} 页`]].map(([value,label])=><label key={value} className={scope===value?'scope-option selected':'scope-option'}><input type="radio" name="task-scope" checked={scope===value} onChange={()=>setScope(value)}/><span>{label}</span></label>)}</div><p className="small muted">使用 {provider==='local'?'本机 Qwen，内容在本机处理。':PROVIDER_NAMES[provider]+(kind==='translate'?' API，所选内容、必要的相邻页内容和本篇相关术语会发送至该服务。':' API，所选内容会发送至该服务。')}</p>{kind==='translate'?<><label className="check-row"><input type="checkbox" checked={force} onChange={e=>setForce(e.target.checked)}/>重新翻译已有译文</label>{force&&<label className="check-row"><input type="checkbox" checked={preserve} onChange={e=>setPreserve(e.target.checked)}/>保留人工修改过的译文</label>}<p className="small muted">一句话跨页时会合起来翻译，必要时更新相邻页的对应段落。重要术语首次出现时保留英文原词。旧译文和笔记会保留。</p></>:<p className="small muted">长文档会逐段阅读后汇总；结果会标明覆盖范围，并提供可核对的原文摘录。</p>}<ErrorLine text={error}/></Modal>
}

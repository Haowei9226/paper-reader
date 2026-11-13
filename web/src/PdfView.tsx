import {useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';
import * as pdfjs from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import 'pdfjs-dist/web/pdf_viewer.css';
import type {PDFDocumentProxy,RenderTask} from 'pdfjs-dist';
import type {Annotation,Counterpart,FindHit,PageData,Selection,Target} from './types';
import {citationLabel,citationRanges,type CitationLinks} from './citations';
import {noteLabel,noteRanges,type NoteLinks} from './noteLinks';
import {Busy,ErrorLine} from './components';
import {locateQuote,textSelection} from './selection';
import {findRects,textRects} from './readerPosition';

pdfjs.GlobalWorkerOptions.workerSrc=workerUrl;
const colors:Record<string,string>={yellow:'#f5cc58',green:'#78c99d',blue:'#82b9f0',pink:'#ed96ae'};

function ocrTextItems(data:PageData, width:number, height:number) {
  const measure = document.createElement('canvas').getContext('2d');
  if (!measure) return [];
  return data.blocks.flatMap(block => [...block.text.matchAll(/\S+ */gu)].flatMap(word => {
    const start = word.index;
    const end = start + word[0].length;
    const text = word[0].trimEnd();
    const [box] = textRects(block, start, start + text.length);
    if (!box) return [];
    const [x0,y0,x1,y1] = box;
    const fontSize = (y1-y0)/data.height*height;
    if (fontSize <= 0 || x1 <= x0) return [];
    measure.font = `${fontSize}px Arial, sans-serif`;
    const naturalWidth = measure.measureText(text).width;
    if (!naturalWidth) return [];
    return [{blockId:block.id, start, end, text:word[0], style:{
      left:`${x0/data.width*100}%`, top:`${y0/data.height*100}%`,
      fontFamily:'Arial, sans-serif', fontSize, lineHeight:1,
      transform:`scaleX(${(x1-x0)/data.width*width/naturalWidth})`,
    }}];
  }));
}

export default function PdfView({paperId,pageNumber,data,annotations,hit,preview,onLayout,onSelect,onAnnotation,citations,noteLinks}:{paperId:string;pageNumber:number;data:PageData|null;annotations:Annotation[];hit?:FindHit;preview?:Counterpart;onLayout:()=>void;onSelect:(s:Selection)=>void;onAnnotation:(a:Annotation)=>void;citations?:CitationLinks;noteLinks?:NoteLinks}){
  const holder=useRef<HTMLDivElement>(null),canvas=useRef<HTMLCanvasElement>(null),layer=useRef<HTMLDivElement>(null),ocrLayer=useRef<HTMLDivElement>(null);
  const [doc,setDoc]=useState<PDFDocumentProxy|null>(null),[width,setWidth]=useState(0),[dimensions,setDimensions]=useState({width:600,height:800,page:0}),[ready,setReady]=useState(false),[error,setError]=useState('');
  const [citationHover,setCitationHover]=useState(false);
  const laidOut = dimensions.page===pageNumber && Math.abs(dimensions.width-width)<.5;
  const ocrItems = useMemo(() => data?.ocr && laidOut ? ocrTextItems(data,dimensions.width,dimensions.height) : [],
    [data,laidOut,dimensions]);
  const markers = useMemo(() => !data ? [] : data.blocks.flatMap(block => {
    const notes=noteLinks?noteRanges(block,block.text,noteLinks):[];
    return [
      ...notes.map(range=>({...range,label:noteLabel(range.noteIds,noteLinks),open:()=>noteLinks?.open(range.noteIds)})),
      ...(citations?citationRanges(block,block.text,citations,notes).map(range=>({...range,label:citationLabel(range.numbers,citations),open:()=>citations.open(range.numbers)})):[]),
    ].map(marker=>({...marker,key:block.id+':'+marker.start,rects:textRects(block,marker.start,marker.end)})).filter(marker=>marker.rects.length);
  }),[data,citations,noteLinks]);
  useLayoutEffect(()=>{if(laidOut)onLayout()},[laidOut,dimensions,onLayout]);
  useEffect(()=>{const el=holder.current;if(!el)return;const observer=new ResizeObserver(entries=>setWidth(Math.max(180,Math.floor(entries[0].contentRect.width))));observer.observe(el);return()=>observer.disconnect()},[]);
  useEffect(()=>{setDoc(null);setError('');const task=pdfjs.getDocument({url:`/api/papers/${paperId}/file`,isEvalSupported:false,useSystemFonts:false,cMapUrl:'/pdf-assets/cmaps/',cMapPacked:true,standardFontDataUrl:'/pdf-assets/standard_fonts/'});let active=true;task.promise.then(d=>{if(active)setDoc(d)}).catch(()=>{if(active)setError('PDF 无法显示，请下载原始文件检查是否有密码或文件损坏')});return()=>{active=false;void task.destroy()}},[paperId]);
  useEffect(()=>{if(!doc||!width||!canvas.current||!layer.current)return;let active=true,render:RenderTask|undefined,textLayer:pdfjs.TextLayer|undefined;setReady(false);setError('');
    void (async()=>{try{const page=await doc.getPage(pageNumber);if(!active)return;const base=page.getViewport({scale:1});const scale=width/base.width;const view=page.getViewport({scale});setDimensions({width:view.width,height:view.height,page:pageNumber});const c=canvas.current!,t=layer.current!;const ratio=Math.min(window.devicePixelRatio||1,2);c.width=Math.floor(view.width*ratio);c.height=Math.floor(view.height*ratio);c.style.width=`${view.width}px`;c.style.height=`${view.height}px`;const context=c.getContext('2d')!;render=page.render({canvasContext:context,viewport:view,transform:ratio!==1?[ratio,0,0,ratio,0,0]:undefined});await render.promise;if(!active)return;t.replaceChildren();t.style.setProperty('--scale-factor',String(scale));t.style.setProperty('--total-scale-factor',String(scale));t.style.width=`${view.width}px`;t.style.height=`${view.height}px`;const content=await page.getTextContent();if(!active)return;textLayer=new pdfjs.TextLayer({textContentSource:content,container:t,viewport:view});await textLayer.render();if(active)setReady(true)}catch(e){if(active&&!(e instanceof Error&&e.name==='RenderingCancelledException'))setError('这一页无法渲染，请切换 HTML 或下载原始文件查看')}})();
    return()=>{active=false;render?.cancel();textLayer?.cancel()}
  },[doc,pageNumber,width]);
  function selectText(){
    if (data?.ocr && ocrLayer.current) {
      const selected = textSelection(ocrLayer.current,data.blocks);
      if (!selected) return;
      // Individual words are positioned separately, but an annotation addresses source block ranges.
      const ranges = new Map<string,Target>();
      for (const target of selected.targets) {
        const previous = ranges.get(target.block_id);
        ranges.set(target.block_id, previous ? {...previous,end:target.end} : target);
      }
      const blocks = new Map(data.blocks.map(block => [block.id,block]));
      const targets = [...ranges.values()].flatMap(target => {
        const quote = blocks.get(target.block_id)?.text.slice(target.start,target.end).trimEnd();
        return quote?.trim() ? [{...target,end:target.start+quote.length,quote}] : [];
      });
      if (targets.length) onSelect({...selected,targets,text:targets.map(target=>target.quote).join('\n')});
      return;
    }
    const selection=window.getSelection();if(!ready||!data||!layer.current||!selection?.rangeCount||selection.isCollapsed||!selection.toString().trim())return;
    const range=selection.getRangeAt(0);if(!layer.current.contains(range.startContainer)||!layer.current.contains(range.endContainer))return;
    const bounds=layer.current.getBoundingClientRect();const sx=bounds.width/data.width,sy=bounds.height/data.height;
    const rects=Array.from(range.getClientRects()).filter(r=>r.width>.1&&r.height<bounds.height*.15).map(r=>[(r.left-bounds.left)/sx,(r.top-bounds.top)/sy,(r.right-bounds.left)/sx,(r.bottom-bounds.top)/sy]);
    const targets:Target[]=[];
    for(const block of data.blocks){let first=-1,last=-1;for(let i=0;i<block.geometry.boxes.length;i++){const b=block.geometry.boxes[i];if(!b)continue;const x=(b[0]+b[2])/2,y=(b[1]+b[3])/2;if(rects.some(r=>x>=r[0]-.3&&x<=r[2]+.3&&y>=r[1]-1&&y<=r[3]+1)){if(first<0)first=i;last=i+1}}if(first>=0){targets.push({block_id:block.id,start:first,end:last,quote:block.text.slice(first,last),page:data.number})}}
    if(!targets.length)return;
    if(targets.length===1){const target=targets[0],block=data.blocks.find(b=>b.id===target.block_id)!;const exact=locateQuote(block.text,selection.toString(),target.start);if(exact){target.start=exact.start;target.end=exact.end;target.quote=block.text.slice(exact.start,exact.end)}}
    const box=range.getBoundingClientRect();onSelect({side:'source',targets,translated_targets:[],text:targets.map(t=>t.quote).join('\n'),x:Math.max(12,Math.min(window.innerWidth-280,box.left)),y:box.bottom+8,top:box.top});
  }
  function pdfPoint(e:React.MouseEvent){
    const box=layer.current?.getBoundingClientRect();
    if (!data || !box?.width || !box.height) return null;
    return {x:(e.clientX-box.left)*data.width/box.width,y:(e.clientY-box.top)*data.height/box.height};
  }
  function markerAt(point:{x:number;y:number}){
    if (!ready || !laidOut) return;
    return markers.find(marker => marker.rects.some(([x0,y0,x1,y1]) => point.x>=x0 && point.x<=x1 && point.y>=y0 && point.y<=y1));
  }
  function clickPage(e:React.MouseEvent){
    if(window.getSelection()?.toString().trim())return;
    const point=pdfPoint(e);
    if(!point)return;
    const marker=markerAt(point);
    if(marker){marker.open();return}
    const {x,y}=point;
    const ann=annotations.find(a=>a.targets.some(t=>t.page===pageNumber&&t.rects?.some(r=>x>=r[0]&&x<=r[2]&&y>=r[1]&&y<=r[3])));
    if(ann)onAnnotation(ann);
  }
  function trackCitation(e:React.MouseEvent){
    if (!markers.length && !citationHover) return;
    const point=pdfPoint(e);
    setCitationHover(!!(point && markerAt(point)));
  }
  return <div className={'pdf-holder' + (citationHover ? ' citation-hover' : '')} ref={holder}
    onMouseUp={()=>window.setTimeout(selectText,0)} onClick={clickPage} onMouseMove={trackCitation} onMouseLeave={()=>setCitationHover(false)}>
    {error&&<ErrorLine text={error}/>}{!ready&&!error&&<div className="pdf-loading"><Busy text="正在显示 PDF"/></div>}
    <div className="pdf-page" data-laid-out-page={laidOut?pageNumber:undefined} style={{width:dimensions.width,height:dimensions.height}}>
      <canvas ref={canvas}/><svg className="pdf-highlights" viewBox={`0 0 ${data?.width||1} ${data?.height||1}`} aria-hidden="true">{annotations.flatMap(a=>a.targets.filter(t=>t.page===pageNumber).flatMap((t,i)=>(t.rects||[]).map((r,j)=><rect key={`${a.id}-${i}-${j}`} x={r[0]} y={r[1]} width={r[2]-r[0]} height={r[3]-r[1]} fill={colors[a.color]} fillOpacity=".35"/>)))}
      {laidOut&&findRects(data,hit).map((r,i)=><rect className="pdf-find-highlight" key={'find-'+i} x={r[0]} y={r[1]} width={r[2]-r[0]} height={r[3]-r[1]}/>)}
      {laidOut&&preview?.side==='source'&&preview.ranges.filter(range=>range.page===pageNumber).flatMap((range,i)=>(range.rects??[]).map((r,j)=><rect className="pdf-selection-match" data-selection-match="" key={`preview-${i}-${j}`} x={r[0]} y={r[1]} width={r[2]-r[0]} height={r[3]-r[1]}/>))}
      {ready&&laidOut&&markers.flatMap(marker=>marker.rects.map(([x0,,x1,y1],index)=>
        <line className="pdf-citation-line" key={marker.key + ':' + index} x1={x0} x2={x1} y1={y1-.45} y2={y1-.45}/>))}
      </svg><div ref={layer} className="textLayer" style={{visibility:ready&&laidOut&&!data?.ocr?'visible':'hidden'}}/>
      {data?.ocr&&ready&&laidOut&&<div ref={ocrLayer} className="textLayer">
        {ocrItems.map(({blockId,start,end,text,style}) => <span key={`${blockId}:${start}`} style={style}
          data-text-block="" data-block-id={blockId} data-text-start={start} data-text-end={end}>{text}</span>)}
      </div>}
      {ready&&laidOut&&data&&markers.map(marker => {
        const [x0,y0,x1,y1]=marker.rects[0];
        return <button key={marker.key} type="button" role="link" className="pdf-citation-target" aria-label={marker.label}
          style={{left:`${x0/data.width*100}%`,top:`${y0/data.height*100}%`,width:`${(x1-x0)/data.width*100}%`,height:`${(y1-y0)/data.height*100}%`}}
          onClick={event=>{event.stopPropagation();window.getSelection()?.removeAllRanges();marker.open()}}/>;
      })}
    </div>
    {!data&&ready&&<p className="small muted pdf-hint">正文解析完成后，即可在 PDF 上创建同步标注。</p>}
  </div>
}

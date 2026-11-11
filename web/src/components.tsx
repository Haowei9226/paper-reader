import {useEffect,useRef,type ReactNode} from 'react';
import {X,LoaderCircle} from 'lucide-react';
import {PUBLICATION_STATUS_LABELS,type PublicationStatus} from './types';

export function IconButton({label,children,onClick,active=false,disabled=false,className=''}:{label:string;children:ReactNode;onClick:()=>void;active?:boolean;disabled?:boolean;className?:string}) {
  return <button type="button" title={label} aria-label={label} aria-pressed={active} disabled={disabled} onClick={onClick} className={`icon-button ${active?'active':''} ${className}`}>{children}</button>;
}
export function Modal({title,children,onClose,wide=false,footer}:{title:string;children:ReactNode;onClose:()=>void;wide?:boolean;footer?:ReactNode}){
  const ref=useRef<HTMLDivElement>(null),closeRef=useRef(onClose);closeRef.current=onClose;
  useEffect(()=>{const previous=document.activeElement as HTMLElement|null; const timer=window.setTimeout(()=>(ref.current?.querySelector<HTMLElement>('input,textarea,select')||ref.current?.querySelector<HTMLElement>('button'))?.focus(),20);const key=(e:KeyboardEvent)=>{if(e.key==='Escape')closeRef.current();if(e.key==='Tab'){const all=ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]');if(!all?.length)return;const first=all[0],last=all[all.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}};document.addEventListener('keydown',key);return()=>{clearTimeout(timer);document.removeEventListener('keydown',key);previous?.focus()}},[]);
  return <div className="modal-overlay" onMouseDown={e=>{if(e.target===e.currentTarget)onClose()}}><div role="dialog" aria-modal="true" aria-label={title} ref={ref} className={`modal ${wide?'wide':''}`}><div className="modal-head"><h2>{title}</h2><IconButton label="关闭对话框" onClick={onClose}><X size={19}/></IconButton></div><div className="modal-body">{children}</div>{footer&&<div className="modal-footer">{footer}</div>}</div></div>
}
export function Busy({text='正在加载'}:{text?:string}){return <div className="busy"><LoaderCircle size={19} className="spin"/><span>{text}</span></div>}
export function PublicationBadge({status}:{status?:PublicationStatus}) {
  return status ? <span className="publication-badge" data-publication-status={status}>{PUBLICATION_STATUS_LABELS[status]}</span> : null;
}
export function ErrorLine({text}:{text:string}){return text?<p className="error-line" role="alert">{text}</p>:null}
export function Empty({icon,title,text,children}:{icon:ReactNode;title:string;text:string;children?:ReactNode}){return <div className="empty-state"><div className="empty-icon">{icon}</div><h2>{title}</h2><p>{text}</p>{children}</div>}

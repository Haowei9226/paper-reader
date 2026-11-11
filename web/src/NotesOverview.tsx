import type {RefObject} from 'react';
import {ChevronRight,FileText,Highlighter} from 'lucide-react';
import {date} from './api';
import {Busy,Empty,ErrorLine} from './components';
import type {NotePaperGroup} from './PaperNotes';
import './notes-library.css';

type Props = {
  groups: NotePaperGroup[];
  total: number;
  query: string;
  loading: boolean;
  error: string;
  hidden: boolean;
  listRef: RefObject<HTMLDivElement|null>;
  page: number;
  pageSize: number;
  onPage: (page:number)=>void;
  onRetry: ()=>void;
  onSelect: (group:NotePaperGroup)=>void;
};

export default function NotesOverview({groups,total,query,loading,error,hidden,listRef,page,pageSize,onPage,onRetry,onSelect}:Props) {
  const pages = Math.max(1,Math.ceil(total/pageSize));
  return <div className="notes-overview" hidden={hidden}>
    <div className="library-titlebar">
      <div><h1>所有笔记</h1><span className="library-count">{total} 篇{query?'匹配论文':'论文'}</span></div>
    </div>
    {error&&<div className="library-error"><ErrorLine text={error}/><button className="text-button" onClick={onRetry}>重试</button></div>}
    <div className="note-group-list" ref={listRef} aria-busy={loading}>
      {groups.length>0 ? <ul>
        {groups.map(group=><li key={group.paper_id}>
          <button type="button" className="note-paper-row" data-note-paper={group.paper_id}
            disabled={loading||!!error} onClick={()=>onSelect(group)}>
            <FileText size={19} className="paper-file-icon" aria-hidden="true"/>
            <span className="note-paper-description">
              <strong>{group.title}</strong>
              {(group.authors||group.year)&&<small>{[group.authors,group.year].filter(Boolean).join(' · ')}</small>}
            </span>
            <span className="note-paper-stats">
              <span>{query?`匹配 ${group.matched} 条 / 共 ${group.total} 条`:`${group.total} 条记录`}</span>
              <time dateTime={new Date(group.updated*1000).toISOString()}>更新于 {date(group.updated)}</time>
            </span>
            <ChevronRight size={15} aria-hidden="true"/>
          </button>
        </li>)}
      </ul> : loading ? <Busy text="正在读取笔记"/> : !error && <Empty
        icon={<Highlighter size={30}/>}
        title={query?'没有找到匹配的笔记':'这里会按论文汇总阅读记录'}
        text={query?'试试论文标题、作者、笔记正文或摘录中的词。':'打开任意论文，在阅读器中标注选段或新建独立笔记。'}
      />}
      {pages>1&&<nav className="table-pagination" aria-label="笔记论文分页">
        <button className="button" disabled={loading||page===1} onClick={()=>onPage(page-1)}>上一页</button>
        <span>第 {page} / {pages} 页</span>
        <button className="button" disabled={loading||page>=pages} onClick={()=>onPage(page+1)}>下一页</button>
      </nav>}
    </div>
  </div>;
}

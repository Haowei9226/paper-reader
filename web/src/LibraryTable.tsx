import {useRef,type KeyboardEvent,type MouseEvent} from 'react';
import {ArrowDown,ArrowUp,BookOpen,ChevronsUpDown,FileText,LoaderCircle,Paperclip,Star} from 'lucide-react';
import {Modal,PublicationBadge} from './components';
import {ITEM_TYPE_LABELS,STATUS_LABELS,type Paper} from './types';

const libraryColumns = [
  {key:'authors', label:'作者', width:150, first:'asc'},
  {key:'item_type', label:'条目类型', width:90, first:'asc'},
  {key:'publication_date', label:'出版日期', width:96, first:'desc'},
  {key:'year', label:'年份', width:62, first:'desc'},
  {key:'publisher', label:'出版社', width:150, first:'asc'},
  {key:'source', label:'出版物', width:190, first:'asc'},
  {key:'last_read_at', label:'最后阅读时间', width:138, first:'desc'},
  {key:'created', label:'添加时间', width:138, first:'desc'},
  {key:'has_attachment', label:'附件', width:60},
  {key:'annotations_count', label:'笔记', width:58, first:'desc'},
  {key:'translated_count', label:'已译段数', width:76, first:'desc'},
  {key:'pages', label:'页数', width:58, first:'desc'},
  {key:'status', label:'状态', width:85, first:'asc'},
] as const;

export type LibraryColumn = typeof libraryColumns[number]['key'];
type SortField = 'title'|'updated'|Exclude<LibraryColumn,'has_attachment'>;
export type LibrarySort = {field:SortField;direction:'asc'|'desc';label:string};
export const librarySortOptions:LibrarySort[] = [
  {field:'updated', direction:'desc', label:'最近阅读与更新'},
  {field:'updated', direction:'asc', label:'阅读与更新时间升序'},
  {field:'title', direction:'asc', label:'标题升序'},
  {field:'title', direction:'desc', label:'标题降序'},
  ...libraryColumns.flatMap(({key,label,...column}):LibrarySort[] => {
    if (key === 'has_attachment' || !('first' in column)) return [];
    const first = column.first;
    return [
      {field:key, direction:first, label:key==='created'?'最近添加':label+(first==='asc'?'升序':'降序')},
      {field:key, direction:first==='asc'?'desc':'asc', label:key==='created'?'最早添加':label+(first==='asc'?'降序':'升序')},
    ];
  }),
];
export const sortValue = (sort:LibrarySort) => `${sort.field}:${sort.direction}`;
const sortStorageKey = 'reader.librarySort';
const columnsStorageKey = 'reader.libraryColumns';
const defaultColumns:LibraryColumn[] = ['authors','year','status'];

export function restoreLibrarySort() {
  try {
    const saved = window.localStorage.getItem(sortStorageKey);
    return librarySortOptions.find(option => sortValue(option) === saved) ?? librarySortOptions[0];
  } catch {
    return librarySortOptions[0];
  }
}

export function rememberLibrarySort(sort:LibrarySort) {
  try { window.localStorage.setItem(sortStorageKey, sortValue(sort)); } catch {}
}

export function restoreLibraryColumns():LibraryColumn[] {
  try {
    const value:unknown = JSON.parse(window.localStorage.getItem(columnsStorageKey) ?? 'null');
    if (Array.isArray(value)) return libraryColumns.map(column=>column.key).filter(key=>value.includes(key));
  } catch {}
  return defaultColumns;
}

export function rememberLibraryColumns(columns:LibraryColumn[]) {
  try { window.localStorage.setItem(columnsStorageKey, JSON.stringify(columns)); } catch {}
}

export function LibraryColumnsDialog({columns,onChange,onClose}:{
  columns:LibraryColumn[];onChange:(columns:LibraryColumn[])=>void;onClose:()=>void;
}) {
  return <Modal title="自定义表头" onClose={onClose} footer={<>
    <button type="button" className="button" onClick={()=>onChange(defaultColumns)}>恢复默认</button>
    <button type="button" className="button primary" onClick={onClose}>完成</button>
  </>}>
    <p className="small muted">勾选要显示的列。标题始终显示，设置会在此浏览器中记住。</p>
    <div className="check-list library-column-options">
      {libraryColumns.map(({key,label})=><label key={key}>
        <input type="checkbox" checked={columns.includes(key)}
          onChange={e=>onChange(e.target.checked?[...columns,key]:columns.filter(column=>column!==key))}/>{label}
      </label>)}
    </div>
  </Modal>;
}

const readingDate = new Intl.DateTimeFormat('zh-CN',{
  year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',
  hourCycle:'h23',timeZone:'Europe/London',
});

function CellValue({paper,column}: {paper:Paper;column:LibraryColumn}) {
  if (column==='status') {
    if (paper.parse_state==='pending'||paper.parse_state==='processing') return <span className="parse-state">
      <LoaderCircle size={12} className="spin"/>转换中
    </span>;
    if (paper.parse_state==='failed') return <span className="parse-state failed">解析待处理</span>;
    return <span className={'read-state '+paper.status}><i/>{STATUS_LABELS[paper.status]}</span>;
  }
  if (column==='has_attachment') return paper.has_attachment
    ? <a className="library-attachment" href={'/api/papers/'+paper.id+'/file'} target="_blank" rel="noreferrer"
        aria-label={'打开 PDF '+paper.title} title={paper.filename} onClick={e=>e.stopPropagation()}><Paperclip size={14}/>PDF</a>
    : '—';

  let value:string|number|null = paper[column];
  if (column==='item_type') value = ITEM_TYPE_LABELS[paper.item_type];
  if (column==='publication_date') value = paper.publication_date || paper.year;
  if (column==='created'||column==='last_read_at') value = value ? readingDate.format(Number(value)*1000) : null;
  if (column==='pages'&&!paper.pages) value = null;
  return <span className="library-cell-value" title={String(value??'')}>{value === '' ? '—' : value ?? '—'}</span>;
}

type Props = {
  papers:Paper[];
  columns:LibraryColumn[];
  sort:LibrarySort;
  focused:string|null;
  checked:ReadonlySet<string>;
  selectable:boolean;
  onSort:(sort:LibrarySort)=>void;
  onColumns:()=>void;
  onFocus:(id:string)=>void;
  onCheck:(ids:Set<string>)=>void;
  onOpen:(paper:Paper)=>void;
  onStar:(paper:Paper)=>void;
};

export default function LibraryTable({papers,columns,sort,focused,checked,selectable,onSort,onColumns,onFocus,onCheck,onOpen,onStar}:Props) {
  const anchor = useRef<string|null>(null);
  const visibleColumns = libraryColumns.filter(column=>columns.includes(column.key));
  const allChecked = papers.every(paper=>checked.has(paper.id));
  const someChecked = papers.some(paper=>checked.has(paper.id));
  const minWidth = (selectable ? 32 : 0) + 40 + 225 + visibleColumns.reduce((width,column)=>width+column.width,0);

  function toggle(paper:Paper) {
    const next = new Set(checked);
    if (next.has(paper.id)) next.delete(paper.id);
    else next.add(paper.id);
    anchor.current = paper.id;
    onCheck(next);
    onFocus(paper.id);
  }

  function select(index:number,event:Pick<MouseEvent,'shiftKey'|'ctrlKey'|'metaKey'>) {
    const id = papers[index].id;
    const additive = event.ctrlKey||event.metaKey;
    if (selectable && event.shiftKey) {
      const start = papers.findIndex(paper=>paper.id===(anchor.current??focused));
      const from = start < 0 ? index : start;
      const next = additive ? new Set(checked) : new Set<string>();
      for (const paper of papers.slice(Math.min(from,index),Math.max(from,index)+1)) next.add(paper.id);
      onCheck(next);
    } else {
      anchor.current = id;
      if (selectable && additive) {
        const next = new Set(checked.size ? checked : focused && focused!==id ? [focused] : []);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        onCheck(next);
      } else if (checked.size) {
        onCheck(new Set());
      }
    }
    onFocus(id);
  }

  function rowKey(event:KeyboardEvent<HTMLTableRowElement>,paper:Paper,index:number) {
    if (event.target!==event.currentTarget) return;
    if (event.key==='Enter') {
      event.preventDefault();
      onOpen(paper);
    } else if (event.key===' '&&selectable) {
      event.preventDefault();
      toggle(paper);
    } else if (event.key==='ArrowDown'||event.key==='ArrowUp') {
      event.preventDefault();
      const step = event.key==='ArrowDown' ? 1 : -1;
      const next = papers[index+step];
      if (!next) return;
      select(index+step,event);
      const row = step===1 ? event.currentTarget.nextElementSibling : event.currentTarget.previousElementSibling;
      if (row instanceof HTMLElement) row.focus();
    }
  }

  function sortHeader(field:SortField,label:string) {
    const direction = sort.field===field ? sort.direction : undefined;
    const next = librarySortOptions.find(option=>option.field===field && option.direction!==direction);
    const Indicator = direction==='asc' ? ArrowUp : direction==='desc' ? ArrowDown : ChevronsUpDown;
    return <th key={field} scope="col" aria-sort={direction==='asc'?'ascending':direction==='desc'?'descending':undefined}>
      <button type="button" className="library-sort-header" onClick={()=>{if(next)onSort(next);}}>
        {label}<Indicator size={12} aria-hidden="true"/>
      </button>
    </th>;
  }

  function checkPage() {
    onCheck(allChecked?new Set():new Set(papers.map(paper=>paper.id)));
  }

  return <table className="library-table configurable-table" style={{minWidth}} onKeyDown={event=>{
    if (!selectable) return;
    if ((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a') {
      event.preventDefault();
      onCheck(new Set(papers.map(paper=>paper.id)));
    } else if (event.key==='Escape'&&checked.size) {
      onCheck(new Set());
    }
  }}>
    <colgroup>
      {selectable&&<col style={{width:32}}/>}<col style={{width:40}}/><col/>
      {visibleColumns.map(({key,width})=><col key={key} style={{width}}/>)}
    </colgroup>
    <thead onContextMenu={event=>{event.preventDefault();onColumns();}}><tr>
      {selectable&&<th className="paper-check-column" scope="col"><input type="checkbox" aria-label="选择当前页"
        ref={element=>{if(element)element.indeterminate=someChecked&&!allChecked;}} checked={allChecked} onChange={checkPage}/></th>}
      <th className="star-column" scope="col" aria-label="星标"/>
      {sortHeader('title','标题')}
      {visibleColumns.map(({key,label})=>key==='has_attachment'?<th key={key} scope="col">{label}</th>:sortHeader(key,label))}
    </tr></thead>
    <tbody>{papers.map((paper,index)=><tr data-paper-row={paper.id} key={paper.id} tabIndex={0} draggable
      className={someChecked ? (checked.has(paper.id)?'selected':'') : focused===paper.id?'selected':''}
      onDragStart={event=>{event.dataTransfer.setData('application/x-reader-paper',paper.id);event.dataTransfer.effectAllowed='copy';}}
      onClick={event=>select(index,event)}
      onDoubleClick={event=>{
        if (!event.shiftKey&&!event.ctrlKey&&!event.metaKey&&!(event.target as Element).closest('button,input,a')) onOpen(paper);
      }}
      onKeyDown={event=>rowKey(event,paper,index)}>
      {selectable&&<td className="paper-check-column" onClick={event=>event.stopPropagation()}>
        <input type="checkbox" aria-label={'选择 '+paper.title} checked={checked.has(paper.id)} onChange={()=>toggle(paper)}/>
      </td>}
      <td className="star-column"><button type="button" className={'table-star '+(paper.starred?'starred':'')}
        title={paper.starred?'取消星标':'添加星标'} aria-label={paper.starred?'取消星标':'添加星标'}
        onClick={event=>{event.stopPropagation();onStar(paper);}}><Star size={15} fill={paper.starred?'currentColor':'none'}/></button></td>
      <td className="paper-title-cell"><div><FileText size={19} className="paper-file-icon"/>
        <span><strong title={paper.title}><PublicationBadge status={paper.publication_status}/>{paper.title}</strong>
          <small>{columns.includes('source')?paper.filename:paper.source||paper.filename}
            {paper.tags.length>0&&<span className="row-tag">{paper.tags[0]}</span>}
          </small>
        </span>
        <button type="button" className="row-open" title="打开文献" aria-label={'打开文献 '+paper.title}
          onClick={event=>{event.stopPropagation();onOpen(paper);}}><BookOpen size={16}/></button>
      </div></td>
      {visibleColumns.map(({key})=><td key={key} className="library-data-cell"><CellValue paper={paper} column={key}/></td>)}
    </tr>)}</tbody>
  </table>;
}

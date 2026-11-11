import {useEffect, useId, useState} from 'react';
import {BookOpen, Download, Highlighter, Plus, Search} from 'lucide-react';
import {api, post} from './api';
import {Busy, Empty, ErrorLine} from './components';
import {NoteDialog} from './Dialogs';
import {AnnotationList} from './Panels';
import type {Annotation, Paper} from './types';
import './notes.css';

export interface NotePaperGroup {
  paper_id: string;
  title: string;
  authors: string;
  year: string;
  total: number;
  matched: number;
  updated: number;
}

type PaperNotesProps = {
  paper: Paper;
  initialQuery: string;
  revision: number;
  onChange: () => void;
  onOpenAnnotation: (annotation: Annotation) => void;
  onOpenPaper: () => void;
  notify: (message: string) => void;
};

type NotesPage = {items: Annotation[]; total: number};
type NoteSort = 'updated' | 'source';
const pageSize = 30;

export default function PaperNotes({paper, initialQuery, revision, onChange, onOpenAnnotation, onOpenPaper, notify}: PaperNotesProps) {
  const headingId = useId();
  const [query, setQuery] = useState(initialQuery);
  const [sort, setSort] = useState<NoteSort>('updated');
  const [page, setPage] = useState(0);
  const [result, setResult] = useState<{path: string; data: NotesPage} | null>(null);
  const [error, setError] = useState<{path: string; message: string} | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [creating, setCreating] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const keyword = query.trim();
  const params = new URLSearchParams({q: keyword, limit: String(pageSize), offset: String(page * pageSize), sort});
  const path = `/annotation-papers/${encodeURIComponent(paper.id)}?${params}`;
  const notes = result?.path === path ? result.data : null;
  const message = error?.path === path ? error.message : '';
  const pageCount = Math.max(1, Math.ceil((notes?.total ?? 0) / pageSize));
  const metadata = [paper.authors, paper.year].filter(Boolean).join(' · ');

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => {
      void api<NotesPage>(path, {signal: controller.signal}).then(data => {
        if (controller.signal.aborted) return;
        setError(null);
        const lastPage = Math.max(0, Math.ceil(data.total / pageSize) - 1);
        if (page > lastPage) setPage(lastPage);
        else setResult({path, data});
      }).catch((e: Error) => {
        if (!controller.signal.aborted) setError({path, message: e.message});
      });
    }, 140);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [path, page, revision, attempt]);

  async function create(note: string, color: string) {
    const created = await post<Annotation>(`/papers/${encodeURIComponent(paper.id)}/annotations`, {note, color});
    setQuery('');
    setSort('updated');
    setPage(0);
    setSelectedId(created.id);
    onChange();
    notify('笔记已保存');
  }

  return <section className="paper-notes" aria-labelledby={headingId}>
    <header className="paper-notes-heading">
      <div>
        <h2 id={headingId}>{paper.title}</h2>
        {metadata && <p>{metadata}</p>}
      </div>
      <div className="paper-notes-actions">
        <button className="button" onClick={onOpenPaper}><BookOpen size={15}/>打开论文</button>
        <a className="button" href={`/api/papers/${encodeURIComponent(paper.id)}/export/notes`}>
          <Download size={15}/>导出笔记
        </a>
        <button className="button primary" onClick={() => setCreating(true)}><Plus size={15}/>新笔记</button>
      </div>
    </header>
    <div className="paper-notes-controls">
      <div className="search-field"><Search size={16}/>
        <input type="search" value={query} placeholder="搜索本篇笔记与摘录" aria-label="搜索本篇笔记与摘录"
          onChange={event => {setQuery(event.target.value); setPage(0); setSelectedId(null)}}/>
      </div>
      <select aria-label="笔记排序" value={sort}
        onChange={event => {setSort(event.target.value as NoteSort); setPage(0); setSelectedId(null)}}>
        <option value="updated">最近更新</option>
        <option value="source">原文页码</option>
      </select>
    </div>
    {notes && <p className="paper-notes-count" aria-live="polite">{keyword ? `找到 ${notes.total} 条记录` : `${notes.total} 条阅读记录`}</p>}
    {message && <div className="paper-notes-error">
      <ErrorLine text={message}/>
      <button className="text-button" onClick={() => {setError(null); setAttempt(value => value + 1)}}>重试</button>
    </div>}
    {notes && notes.items.length > 0 && <>
      <AnnotationList annotations={notes.items} selectedId={selectedId} pages={paper.pages} onChange={onChange}
        onOpenAnnotation={onOpenAnnotation} showUpdated/>
      {pageCount > 1 && <nav className="table-pagination" aria-label="本篇笔记分页">
        <button className="button" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
        <span>第 {page + 1} / {pageCount} 页</span>
        <button className="button" disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)}>下一页</button>
      </nav>}
    </>}
    {notes && notes.items.length === 0 && <Empty icon={<Highlighter size={28}/>}
      title={keyword ? '没有找到匹配的笔记' : '这篇论文还没有笔记'}
      text={keyword ? '试试其他关键词。' : '可以在这里写一条笔记，或阅读论文时选中文字做标注。'}/>}
    {!notes && !message && <Busy text="正在读取本篇笔记"/>}
    {creating && <NoteDialog selection={null} onClose={() => setCreating(false)} onSave={create}/>}
  </section>;
}

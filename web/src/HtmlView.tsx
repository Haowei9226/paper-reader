import {Fragment, memo, useRef} from 'react';
import {ChevronLeft, ChevronRight, Languages, Pencil} from 'lucide-react';
import type {Annotation, Block, Counterpart, FindHit, Notation, Paper, ReadingRole, ReadingSide, ReadingTable, Selection, TextAlias, TextBounds} from './types';
import {citationLabel,citationRanges,type CitationLinks} from './citations';
import {textSelection} from './selection';
import '../../reader/reading.css';

const softWrap = /\u00ad(?:[ \t\r\n]+(?=\p{L}))?/gu;
export const readableText = (text: string) => text.replace(softWrap, '');

export type AnnotationIndex = Map<string, Annotation[]>;
const noAnnotations: Annotation[] = [];

export function indexAnnotations(annotations: Annotation[]): AnnotationIndex {
  const index: AnnotationIndex = new Map();
  for (const annotation of annotations) {
    for (const id of new Set([...annotation.targets, ...annotation.translated_targets].map(target => target.block_id))) {
      const list = index.get(id);
      if (list) list.push(annotation);
      else index.set(id, [annotation]);
    }
  }
  return index;
}

function roleOf(block: Block): ReadingRole {
  if (block.reading_role) return block.reading_role;
  if (block.kind === 'image') return 'image';
  if (block.kind === 'heading') return 'heading';
  return 'text';
}

function styledNotation(block: Block, side: ReadingSide): Notation[] {
  const notation = block.notation ?? [];
  if (side === 'source') return notation;
  return (block.translation?.segments ?? []).filter(span => span.notation && !span.target_block_id).flatMap(span => span.source_block_id
    ? span.notation_styles ?? [] : notation
    .filter(([start,end]) => span.source_start <= start && end <= span.source_end)
    .map(([start,end,kind]):Notation => [span.target_start + start - span.source_start, span.target_start + end - span.source_start, kind]));
}

function Highlighted({text, block, side, annotations, hit, preview, onAnnotation, bounds = [0,text.length], aliases, citations, joinsWord}: {
  text: string;
  block: Block;
  side: ReadingSide;
  annotations: Annotation[];
  hit?: FindHit;
  preview?: Counterpart;
  bounds?: TextBounds;
  aliases?: TextAlias[];
  citations?: CitationLinks;
  joinsWord?: boolean;
  onAnnotation: (annotation: Annotation) => void;
}) {
  function position(range: {block_id:string;translation_id?:string|null;start:number;end:number}): TextBounds|undefined {
    if (range.block_id === block.id && (side === 'source' || range.translation_id === block.translation?.id)
        && range.start < bounds[1] && range.end > bounds[0]) return [range.start,range.end];
    const alias = aliases?.find(item => range.block_id === item.block_id && range.translation_id === item.translation_id
      && item.start <= range.start && range.end <= item.end);
    if (alias) return [bounds[0] + range.start - alias.start, bounds[0] + range.end - alias.start];
  }
  const ranges = annotations.flatMap(annotation => (side === 'translation' ? annotation.translated_targets : annotation.targets)
    .flatMap(range => {
      const found = position(range);
      return found ? [{start:found[0],end:found[1],annotation}] : [];
    }));
  const endpoints = ranges.flatMap(range => [range.start, range.end].map(point => Math.max(0, Math.min(text.length, point))));
  const temporary = preview?.side === side ? preview.ranges.flatMap(range => {
    const found = position(range);
    return found && text.slice(...found) === range.quote ? [{start:found[0],end:found[1]}] : [];
  }) : [];
  endpoints.push(...temporary.flatMap(range=>[range.start,range.end]));
  const matchBounds = hit?.side === side ? position(hit) : undefined;
  const match = matchBounds && hit && text.slice(...matchBounds) === hit.match
    ? {start:matchBounds[0],end:matchBounds[1]} : undefined;
  if (match) endpoints.push(match.start, match.end);
  // Retain the original UTF-16 text in the DOM Range while hiding PDF line-wrap artifacts.
  const breaks = side === 'source' ? [...text.matchAll(softWrap)].map(m => [m.index!, m.index! + m[0].length]) : [];
  if (joinsWord && /\s$/u.test(text)) breaks.push([text.trimEnd().length,text.length]);
  const bullets = new Set<number>();
  if (roleOf(block) === 'table') {
    for (const match of text.slice(bounds[0], bounds[1]).matchAll(/\s+(?=[•◦●▪▫])/gu)) {
      const point = bounds[0] + match.index! + match[0].length;
      if (point > bounds[0]) bullets.add(point);
    }
  }
  const styles = styledNotation(block,side);
  endpoints.push(...bullets, ...styles.flatMap(([start,end]) => [start,end]));
  const linked = citations ? citationRanges(block,text,citations)
    .filter(range => bounds[0] <= range.start && range.end <= bounds[1]) : [];
  endpoints.push(...linked.flatMap(range => [range.start,range.end]));
  const points = [...new Set([...bounds, ...endpoints.filter(point => bounds[0] < point && point < bounds[1])])].sort((a, b) => a - b);
  const chunks = points.slice(0, -1).map((start, index) => {
    const end = points[index + 1];
    let cursor = start;
    const content = breaks.filter(([a, b]) => a < end && b > start).map(([a, b]) => {
      const before = text.slice(cursor, Math.max(cursor, a));
      const hidden = text.slice(Math.max(start, a), Math.min(end, b));
      cursor = Math.min(end, b);
      return <Fragment key={a}>{before}<span hidden className="source-break">{hidden}</span></Fragment>;
    });
    const value = <>{bullets.has(start) && <br/>}{content}{text.slice(cursor, end)}</>;
    const style = styles.find(([a,b]) => a <= start && end <= b)?.[2];
    const slice = style === 'sup' ? <sup>{value}</sup> : style === 'sub' ? <sub>{value}</sub>
      : style === 'math' ? <span className="math-inline">{value}</span> : value;
    const range = ranges.find(candidate => candidate.start <= start && candidate.end >= end);
    const found = match && match.start <= start && match.end >= end;
    const previewed = temporary.some(candidate=>candidate.start<=start&&candidate.end>=end);
    const transient = (found ? ' find-highlight' : '') + (previewed ? ' selection-match' : '');
    if (!range && transient) return <mark key={start} className={transient.trim()} data-find-hit={found?'':undefined} data-selection-match={previewed?'':undefined}>{slice}</mark>;
    if (!range) return <span key={start}>{slice}</span>;
    const insideCitation = linked.some(citation => citation.start <= start && end <= citation.end);
    return <mark
      key={start}
      className={'hl-' + range.annotation.color + transient}
      data-find-hit={found ? '' : undefined}
      data-selection-match={previewed ? '' : undefined}
      title={range.annotation.note || '查看标注'}
      role={insideCitation ? undefined : 'button'}
      tabIndex={insideCitation ? undefined : 0}
      onKeyDown={insideCitation ? undefined : event => { if (event.key === 'Enter') onAnnotation(range.annotation); }}
      onClick={insideCitation ? undefined : () => { if (!window.getSelection()?.toString().trim()) onAnnotation(range.annotation); }}
    >{slice}</mark>;
  });
  if (!citations || !linked.length) return <>{chunks}</>;
  const nodes = [];
  let last = 0;
  for (const citation of linked) {
    const first = points.indexOf(citation.start);
    const end = points.indexOf(citation.end);
    const label = citationLabel(citation.numbers,citations);
    nodes.push(...chunks.slice(last,first), <span className="inline-citation" role="link" tabIndex={0}
      key={'citation-' + citation.start} aria-label={label} title={label}
      onClick={() => {if (!window.getSelection()?.toString().trim()) citations.open(citation.numbers)}}
      onKeyDown={event => {if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        window.getSelection()?.removeAllRanges();
        citations.open(citation.numbers);
      }}}>
      {chunks.slice(first,end)}
    </span>);
    last = end;
  }
  return <>{nodes}{chunks.slice(last)}</>;
}

function TextBlock({block, side, language, annotations, onAnnotation, onEdit, flash, hit, preview, paired = false, inline = false, bounds, aliases, citations, joinsWord}: {
  block: Block;
  side: ReadingSide;
  language: string;
  annotations: Annotation[];
  onAnnotation: (annotation: Annotation) => void;
  onEdit?: (block: Block) => void;
  flash?: string;
  hit?: FindHit;
  preview?: Counterpart;
  paired?: boolean;
  inline?: boolean;
  bounds?: TextBounds;
  aliases?: TextAlias[];
  citations?: CitationLinks;
  joinsWord?: boolean;
}) {
  const role = roleOf(block);
  if (role === 'formula') {
    const equation = block.equation;
    if (!equation) return null;
    return <figure className="reading-equation" data-block-anchor={block.id} data-reading-side={side}>
      {equation.block_ids.slice(1).map(id => <span key={id} data-block-anchor={id} data-reading-side={side}/>)}
      <a href={`/api/papers/${block.paper_id}/file#page=${block.page}`} target="_blank" rel="noreferrer" title="在 PDF 中查看公式">
        <img src={'/api/assets/' + equation.asset} alt={equation.number ? `公式 (${equation.number})` : '原文公式'}
          width={Math.round(equation.width * 1.65)} height={Math.round(equation.height * 1.65)} loading="lazy"/>
      </a>
    </figure>;
  }
  if (role === 'image') {
    return <figure className="reading-figure" data-block-anchor={block.id} data-reading-side={side}>
      <img src={'/api/assets/' + block.asset} alt={'第 ' + block.page + ' 页图表'} loading="lazy"
        width={Math.max(1, Math.round((block.bbox[2] - block.bbox[0]) * 1.3))}
        height={Math.max(1, Math.round((block.bbox[3] - block.bbox[1]) * 1.3))}/>
    </figure>;
  }
  const translation = side === 'translation' ? block.translation : null;
  const text = side === 'translation' ? translation?.text : block.text;
  if (!text) {
    const continuation = translation?.segments.some(span => span.target_block_id);
    return <div className="translation-slot article-status" data-block-anchor={block.id} data-reading-side={side} aria-hidden={!continuation}>
      {continuation ? '跨页句已接在前一页的译文中。' : undefined}
    </div>;
  }

  const Tag = inline || bounds ? 'span' : paired ? 'p' : role === 'title' ? 'h2' : role === 'heading' ? 'h3' : role === 'subheading' ? 'h4' : 'p';
  const Wrapper = inline || bounds ? 'span' : 'div';
  const textClass = role === 'title' && !inline ? (paired ? 'article-title-translation' : 'article-title') : 'block-text';
  return <Wrapper
    data-block-anchor={block.id}
    data-reading-side={side}
    lang={language}
    className={'reading-block reading-' + role + (inline ? ' reading-fragment' : '') + (bounds ? ' table-text-part' : '') + (flash === block.id ? ' flash-block' : '')}
  >
    {aliases?.map(alias => <span key={alias.block_id + ':' + alias.start} data-block-anchor={alias.block_id} data-reading-side={side} aria-hidden="true"/>)}
    <Tag
      className={textClass}
      data-text-block=""
      data-block-id={block.id}
      data-side={side}
      data-text-start={bounds?.[0]}
      data-text-end={bounds?.[1]}
      data-text-aliases={aliases?.length ? JSON.stringify(aliases) : undefined}
      data-translation-id={translation?.id}
    >
      <Highlighted text={text} block={block} side={side} annotations={annotations} hit={hit} preview={preview}
        onAnnotation={onAnnotation} bounds={bounds} aliases={aliases} citations={citations} joinsWord={joinsWord}/>
    </Tag>
    {translation && onEdit && !bounds && <button
      className="block-edit"
      title="修改译文或查看历史版本"
      aria-label="修改译文或查看历史版本"
      onClick={() => onEdit(block)}
    ><Pencil size={13}/></button>}
    {translation?.manual === 1 && !bounds && <span className="manual-indicator">已人工修改</span>}
  </Wrapper>;
}

export type HtmlViewProps = {
  paper: Paper;
  page: number;
  blocks: Block[];
  tables?: ReadingTable[];
  citations?: CitationLinks;
  side: ReadingSide;
  annotations: AnnotationIndex;
  onSelect: (selection: Selection) => void;
  onAnnotation: (annotation: Annotation) => void;
  onEdit?: (block: Block) => void;
  fontSize: number;
  flash?: string;
  hit?: FindHit;
  preview?: Counterpart;
  onTranslate: (page: number) => void;
  onPageChange?: (page: number) => void;
  interleaved?: boolean;
  continuous?: boolean;
};

export default memo(function HtmlView({paper, page, blocks, tables = [], citations, side, annotations, onSelect, onAnnotation, onEdit, fontSize, flash, hit, preview, onTranslate, onPageChange, interleaved = false, continuous = false}: HtmlViewProps) {
  const root = useRef<HTMLElement>(null);
  const sourceLanguage = paper.language === 'zh' ? 'zh-CN' : 'en';
  const targetLanguage = sourceLanguage === 'en' ? 'zh-CN' : 'en';
  const titleBlocks = blocks.filter(block => roleOf(block) === 'title');
  const titleBlock = titleBlocks[0];
  const translatedTitle = titleBlocks.length > 0 && titleBlocks.every(block => block.translation?.text);
  const byId = new Map(blocks.map(block => [block.id,block]));
  const tableCaptions = new Set(tables.map(table => table.caption_id));
  const body = blocks.filter(block => !['title', 'extra', 'image', 'caption', 'table', 'embedded'].includes(roleOf(block)) &&
    (roleOf(block) !== 'formula' || block.equation));
  const figures = blocks.filter(block => (roleOf(block) === 'image' || roleOf(block) === 'caption') && !tableCaptions.has(block.id));
  const runs: Block[][] = [];
  for (const block of body) {
    if (block.continues_previous && runs.length) runs[runs.length - 1].push(block);
    else runs.push([block]);
  }
  const extras = blocks.filter(block => roleOf(block) === 'extra' && (side === 'source' || block.translation?.text));
  const prose = body.filter(block => roleOf(block) !== 'formula');
  const total = prose.length;
  const translated = prose.filter(block => block.translation).length;
  const needsTranslation = side === 'translation' && !blocks.some(block => block.translation);
  const publication = [paper.source, paper.year].filter(Boolean).join(' · ');
  const authors = paper.authors.split(/[;；]/).map(name => name.trim()).filter(Boolean).join(' · ');

  function renderBlock(block: Block, blockSide: ReadingSide, paired = false, inline = false, bounds?: TextBounds, aliases?: TextAlias[], joinsWord?: boolean) {
    const related = annotations.get(block.id) ?? noAnnotations;
    const grouped = aliases?.length ? [...new Map([...related, ...aliases.flatMap(alias => annotations.get(alias.block_id) ?? noAnnotations)]
      .map(annotation => [annotation.id,annotation])).values()] : related;
    return <TextBlock
      key={block.id}
      block={block}
      side={blockSide}
      language={blockSide === 'source' ? sourceLanguage : targetLanguage}
      annotations={grouped}
      onAnnotation={onAnnotation}
      onEdit={onEdit}
      flash={flash}
      hit={hit}
      preview={preview}
      paired={paired}
      inline={inline}
      bounds={bounds}
      aliases={aliases}
      citations={citations}
      joinsWord={joinsWord}
    />;
  }

  function renderTable(table: ReadingTable, tableSide: ReadingSide) {
    const caption = table.caption_id ? byId.get(table.caption_id) : undefined;
    const missing = tableSide === 'translation' && table.cells.some(cell => cell.parts.some(part => !part.target));
    const related = table.block_ids.map(id => byId.get(id)).filter((block):block is Block => !!block?.translation);
    const alt = caption ? readableText(caption.text).trim() : `第 ${page} 页原表`;
    const renderRow = (row: number) => <tr key={row}>
      {table.cells.filter(cell => cell.row === row).map(cell => {
        const Tag = row < table.header_rows ? 'th' : 'td';
        const shared = tableSide === 'translation' && cell.joined;
        const aliases = shared ? cell.parts.slice(1).flatMap(part => {
          const translation = byId.get(part.block_id)?.translation;
          return translation && part.target ? [{block_id:part.block_id,translation_id:translation.id,start:part.target[0],end:part.target[1]}] : [];
        }) : undefined;
        return <Tag key={cell.column} rowSpan={cell.rowspan} colSpan={cell.colspan}
          scope={Tag === 'th' ? (cell.colspan > 1 ? 'colgroup' : 'col') : undefined}>
          {(shared ? cell.parts.slice(0,1) : cell.parts).map(part => {
            const block = byId.get(part.block_id);
            if (!block) return null;
            const target = tableSide === 'translation' ? part.target : null;
            return <span key={part.block_id + ':' + part.start} className={tableSide === 'translation' && !target ? 'table-pending' : undefined}>
              {renderBlock(block, target ? 'translation' : 'source', false, false, target ?? [part.start,part.end], aliases)}
            </span>;
          })}
        </Tag>;
      })}
    </tr>;
    return <figure className="reading-table" data-table={table.id} key={table.id + ':' + tableSide}>
      {caption && <figcaption>{renderBlock(caption, tableSide === 'translation' && !caption.translation ? 'source' : tableSide)}</figcaption>}
      {table.cells.length ? <>
        {missing && <p className="article-status table-status">灰色单元格显示原文 <button className="text-button" onClick={() => onTranslate(page)}>翻译这一页</button></p>}
        <div className="reading-table-scroll" tabIndex={0} aria-label={alt}>
          <table style={{minWidth:`${Math.max(12, table.columns * 5.4)}em`}}>
            <colgroup>{table.widths.map((width,index) => <col key={index} style={{width:`${width}%`}}/>)}</colgroup>
            <thead>{Array.from({length:table.header_rows},(_,row) => renderRow(row))}</thead>
            <tbody>{Array.from({length:table.rows - table.header_rows},(_,row) => renderRow(row + table.header_rows))}</tbody>
          </table>
        </div>
        {tableSide === 'source' && <details className="table-original">
          <summary>查看原表</summary>
          <img src={'/api/assets/' + table.asset} alt={alt} loading="lazy"/>
        </details>}
      </> : <>
        <p className="article-status">此表保留 PDF 原版</p>
        <img className="table-original-image" src={'/api/assets/' + table.asset} alt={alt} loading="lazy"/>
      </>}
      {tableSide === 'translation' && related.length > 0 && <details className="table-original">
        <summary>译文历史与人工修改</summary>
        {related.map(block => renderBlock(block, 'translation'))}
      </details>}
    </figure>;
  }

  function renderRun(run: Block[], blockSide: ReadingSide, paired = false) {
    const words = run.map(block => blockSide === 'source' ? block.text : block.translation?.text ?? '');
    const language = blockSide === 'source' ? sourceLanguage : targetLanguage;
    if (run.length === 1 || (blockSide === 'translation' && (words.some(word => !word) || words.slice(0, -1).some(word => /[.!?。！？][\s”’）)\]」』]*$/u.test(word))))) {
      return <Fragment key={run[0].id}>{run.map(block => renderBlock(block, blockSide, paired))}</Fragment>;
    }
    const wordWraps = words.map((word,index) => blockSide === 'source'
      && /[-\u00ad]\s*$/u.test(word) && /^\p{Ll}/u.test(words[index+1] ?? ''));
    return <p className="reading-block reading-joined" lang={language} key={run[0].id}>
      {run.map((block, index) => <Fragment key={block.id}>
        {index > 0 && !wordWraps[index-1] && !language.startsWith('zh') && !/\s$/u.test(words[index-1]) && !/^\s/u.test(words[index]) ? ' ' : ''}
        {renderBlock(block, blockSide, paired, true, undefined, undefined, wordWraps[index])}
      </Fragment>)}
    </p>;
  }

  function renderTitle(blockSide: ReadingSide, paired = false) {
    if (titleBlocks.length === 1) return renderBlock(titleBlocks[0],blockSide,paired);
    const Tag=paired?'p':'h2';
    return <Tag className={paired?'article-title-translation':'article-title'}>
      {titleBlocks.map((block,index)=><Fragment key={block.id}>{index>0?' ':''}{renderBlock(block,blockSide,paired,true)}</Fragment>)}
    </Tag>;
  }

  const flow = [
    ...runs.map(run => ({position:run[0].position, content:interleaved && run.some(block => roleOf(block) !== 'formula' && block.translation)
      ? <div className="paragraph-pair" key={run[0].id}>
          {renderRun(run,'source')}
          <div className="paired-translation" lang={targetLanguage}>{renderRun(run,'translation',true)}</div>
        </div>
      : renderRun(run,side)})),
    ...tables.map(table => ({position:table.position, content:interleaved ? <Fragment key={table.id}>
      {renderTable(table,'source')}
      {table.cells.some(cell => cell.parts.some(part => part.target)) && renderTable(table,'translation')}
    </Fragment> : renderTable(table,side)})),
  ].sort((a,b) => a.position - b.position);

  return <article
    className={'html-content article-view' + (continuous ? ' continuous-page' : '') + (continuous && needsTranslation ? ' untranslated-page' : '')}
    data-reading-page={continuous ? page : undefined}
    style={{fontSize}}
    lang={side === 'source' ? sourceLanguage : targetLanguage}
    ref={root}
    onMouseUp={continuous ? undefined : () => window.setTimeout(() => {
      if (!root.current) return;
      const selection = textSelection(root.current, blocks);
      if (selection) onSelect(selection);
    }, 0)}
  >
    {continuous && page > 1 && <div className="article-page-number continuous-page-label">第 {page} 页</div>}
    {needsTranslation && !continuous && !tables.length && !body.some(block => block.equation) ? <div className="translation-empty">
      <Languages size={24}/>
      <h3>这一页还没有译文</h3>
      <button className="button primary" onClick={() => onTranslate(page)}>翻译当前页</button>
    </div> : <>
      {page === 1 && <header className="article-cover" lang={sourceLanguage}>
        {publication && <p className="article-publication">{publication}</p>}
        {titleBlock && (side === 'source' || translatedTitle)
          ? renderTitle(side)
          : <h2 className="article-title" data-block-anchor={continuous ? titleBlock?.id : undefined} data-reading-side={side}>{paper.title}</h2>}
        {interleaved && translatedTitle && renderTitle('translation', true)}
        {authors && <p className="article-authors">{authors}</p>}
      </header>}
      {(needsTranslation || ((side === 'translation' || interleaved) && translated < total)) && <p className="article-status">
        {needsTranslation ? '本页还没有译文' : `本页正文已译 ${translated} / ${total} 段`}
        <button className="text-button" onClick={() => onTranslate(page)}>{continuous ? '翻译这一页' : '翻译当前页'}</button>
      </p>}
      {!flow.length && <p className="reading-block">这一页暂无可重排的正文。</p>}
      {flow.map(item => item.content)}
      {figures.length > 0 && <details className="article-extras article-figures" open={!body.length ? true : undefined}>
        <summary>本页图表 · {Math.max(figures.filter(block => block.kind === 'image').length, figures.filter(block => roleOf(block) === 'caption').length)}</summary>
        {figures.map(block => <div key={block.id}>
          {renderBlock(block, side)}
          {interleaved && block.translation?.text && <div className="paired-translation" lang={targetLanguage}>{renderBlock(block, 'translation', true)}</div>}
        </div>)}
      </details>}
      {extras.length > 0 && <details className="article-extras">
        <summary>页边与出版信息 · {extras.length} 段</summary>
        {extras.map(block => <div key={block.id}>
          {renderBlock(block, side)}
          {interleaved && block.translation?.text && <div className="paired-translation" lang={targetLanguage}>{renderBlock(block, 'translation', true)}</div>}
        </div>)}
      </details>}
    </>}
    {onPageChange && paper.pages > 1 && <nav className="article-pager" aria-label="译文翻页">
      <button disabled={page <= 1} onClick={() => onPageChange(page - 1)} aria-label="阅读区上一页"><ChevronLeft size={16}/>上一页</button>
      <span>第 {page} / {paper.pages} 页</span>
      <button disabled={page >= paper.pages} onClick={() => onPageChange(page + 1)} aria-label="阅读区下一页">下一页<ChevronRight size={16}/></button>
    </nav>}
  </article>;
});

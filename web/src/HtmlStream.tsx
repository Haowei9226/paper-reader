import {Component, createRef, useEffect, useRef} from 'react';
import {Busy, ErrorLine} from './components';
import HtmlView from './HtmlView';
import type {HtmlViewProps} from './HtmlView';
import {capturePosition, scrollForPosition} from './readerPosition';
import type {Position} from './readerPosition';
import {textSelection} from './selection';
import {noteBlockIds} from './documentNotes';
import {readingContinuations} from './readingFlow';
import type {HtmlPageSlot} from './useHtmlPages';

type StreamPagesProps = Omit<HtmlViewProps, 'page' | 'blocks' | 'tables' | 'onPageChange' | 'continuous'> & {
  pages: HtmlPageSlot[];
  anchorPage: number;
  viewportKey: string;
  retry: (page: number) => void;
  place: (pane: HTMLElement, top: number) => void;
};

type StreamProps = StreamPagesProps & {
  canPrepend: boolean;
  canAppend: boolean;
  extend: (direction: 'before' | 'after') => void;
};

// Take the position before React inserts earlier pages or a fresh translation
// changes their height. Restore the same passage in each pane after the commit.
class StreamPages extends Component<StreamPagesProps> {
  private root = createRef<HTMLDivElement>();

  getSnapshotBeforeUpdate(previous: StreamPagesProps): Position | null {
    if (previous.pages === this.props.pages && previous.fontSize === this.props.fontSize && previous.viewportKey === this.props.viewportKey) return null;
    const pane = this.root.current?.closest<HTMLElement>('.pane-scroll');
    return pane && previous.pages.some(page => page.data) ? capturePosition(pane, previous.anchorPage, previous.side, null) : null;
  }

  componentDidUpdate(_previous: StreamPagesProps, _state: unknown, position: Position | null) {
    if (!position || !this.props.pages.some(page => page.number === position.page && page.data)) return;
    const pane = this.root.current?.closest<HTMLElement>('.pane-scroll');
    if (pane) this.props.place(pane, scrollForPosition(pane, position, position.side ?? this.props.side, null));
  }

  render() {
    const {pages, anchorPage, viewportKey, retry, place, hit, preview, flash, ...view} = this.props;
    const last = pages[pages.length - 1];
    const excluded = noteBlockIds(pages.flatMap(page => page.data?.blocks ?? []),view.documentNotes);
    const continuations = readingContinuations(pages,excluded);
    return <div
      ref={this.root}
      data-reading-pages=""
      data-document-end={last?.data && last.number === view.paper.pages ? '' : undefined}
      onMouseUp={() => window.setTimeout(() => {
        const root = this.root.current;
        if (!root || !window.getSelection()?.toString().trim()) return;
        const selection = textSelection(root, pages.flatMap(page => page.data?.blocks ?? []));
        if (selection) view.onSelect(selection);
      }, 0)}
    >
      {pages.map(({number, data, error}) => data
        ? <HtmlView key={number} {...view} page={number} blocks={data.blocks} tables={data.tables} continuous
          continuation={continuations.get(number)} hit={hit} preview={preview} flash={flash}/>
        : <section key={number} className="html-content article-view continuous-page stream-page-wait" data-reading-page={number}>
          {error ? <>
            <p className="article-page-number">第 {number} 页暂不可读</p>
            <ErrorLine text={error}/>
            <button className="button compact" onClick={() => retry(number)}>重试</button>
          </> : <Busy text={`正在载入第 ${number} 页`}/>}
        </section>)}
    </div>;
  }
}

export default function HtmlStream({canPrepend, canAppend, extend, ...props}: StreamProps) {
  const top = useRef<HTMLDivElement>(null), bottom = useRef<HTMLDivElement>(null);
  const {pages, paper} = props;
  const first = pages[0]?.number, last = pages[pages.length - 1];
  const extendRef = useRef(extend);
  extendRef.current = extend;
  useEffect(() => {
    const pane = top.current?.closest('.pane-scroll');
    if (!pane) return;
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) extendRef.current(entry.target === top.current ? 'before' : 'after');
      }
    }, {root: pane, rootMargin: '360px 0px 560px'});
    if (canPrepend && top.current) observer.observe(top.current);
    if (canAppend && bottom.current) observer.observe(bottom.current);
    return () => observer.disconnect();
  }, [first, last?.number, canPrepend, canAppend]);

  return <div className="html-flow">
    <div className="html-flow-edge" ref={top} aria-hidden="true"/>
    <StreamPages {...props}/>
    <div className="html-flow-edge" ref={bottom} aria-hidden="true"/>
    {last?.data && last.number === paper.pages && <p className="html-flow-end">已到文末</p>}
  </div>;
}
